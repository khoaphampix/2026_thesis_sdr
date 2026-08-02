#!/usr/bin/env python3
"""
one_pluto_live_camera_v8_9_rtsp_pipe_fix.py

Simulate a live H.265 security camera using one PlutoSDR in TX/RX loopback.

Flow:
    FFmpeg camera simulation
    -> small H.265 byte packets
    -> sequence number + timestamp + CRC
    -> BPSK
    -> Pluto TX
    -> Pluto RX
    -> packet recovery
    -> FFplay live display
    -> saved received H.265 stream

Exactly one adi.Pluto context remains open for the whole run. Recovery first refreshes only the RX buffer while the same cyclic TX packet remains active, then falls back to the existing in-place radio resynchronization.
"""

from __future__ import annotations

import argparse
from collections import deque
import os
import re
import hashlib
import shutil
import struct
import subprocess
import threading
import time
import zlib
from pathlib import Path

try:
    import adi
except ModuleNotFoundError:
    adi = None

import numpy as np


# ---------------------------------------------------------------------------
# Real RTSP camera configuration
# ---------------------------------------------------------------------------
# Replace PASSWORD once with the local camera password.
# The script does not print this URL in its normal terminal output.
CAMERA_URL = (
    'rtsp://admin:cdu_2026@192.168.1.2:554/Preview_01_sub'
)


# ------------------------------------------------------------
# Packet format and synchronization
# ------------------------------------------------------------

MAGIC = b"CAM1"

# magic, sequence, timestamp_ms, payload_length, flags, CRC-32
HEADER_FORMAT = "!4sIIHBI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

FLAG_START = 0x01
FLAG_END = 0x02

# Reproducible 256-bit synchronization pattern.
_rng = np.random.default_rng(20260710)
PREAMBLE_BITS = _rng.integers(0, 2, 512, dtype=np.uint8)

MAGIC_BITS = np.unpackbits(
    np.frombuffer(MAGIC, dtype=np.uint8)
)

# Using preamble + magic makes false synchronization less likely.
SYNC_BITS = np.concatenate((PREAMBLE_BITS, MAGIC_BITS))


# ------------------------------------------------------------
# Basic conversion functions
# ------------------------------------------------------------

def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(
        np.frombuffer(data, dtype=np.uint8)
    )


def bits_to_bytes(bits: np.ndarray) -> bytes:
    bits = np.asarray(bits, dtype=np.uint8)

    if len(bits) % 8 != 0:
        raise ValueError("Bit length must be divisible by 8.")

    return np.packbits(bits).tobytes()


def bits_to_bpsk(bits: np.ndarray) -> np.ndarray:
    # bit 0 -> +1, bit 1 -> -1
    return (
        1.0 - 2.0 * np.asarray(bits, dtype=np.float32)
    ).astype(np.complex64)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        while block := file.read(1024 * 1024):
            digest.update(block)

    return digest.hexdigest()


# ------------------------------------------------------------
# Packet creation and checking
# ------------------------------------------------------------


def balanced_padding(length: int, sequence: int) -> bytes:
    """
    Return deterministic DC-balanced padding for the unused payload area.

    Alternating 0xAA/0x55 bytes contain equal numbers of zero and one bits,
    preventing the final short packet from creating a large BPSK DC bias.
    """
    if length <= 0:
        return b""

    first = 0xAA if (sequence & 1) == 0 else 0x55
    second = 0x55 if first == 0xAA else 0xAA
    pattern = bytes((first, second))

    return (pattern * ((length + 1) // 2))[:length]


def build_packet(
    sequence: int,
    timestamp_ms: int,
    flags: int,
    payload: bytes,
    payload_size: int,
) -> bytes:
    if len(payload) > payload_size:
        raise ValueError("Payload is too large.")

    crc = zlib.crc32(payload) & 0xFFFFFFFF

    header = struct.pack(
        HEADER_FORMAT,
        MAGIC,
        sequence,
        timestamp_ms & 0xFFFFFFFF,
        len(payload),
        flags,
        crc,
    )

    # Fixed packet size makes SDR recovery simpler. Use balanced padding
    # instead of zeros so the final short packet remains DC-balanced.
    padding = balanced_padding(
        payload_size - len(payload),
        sequence=sequence,
    )
    return header + payload + padding


def parse_packet(packet: bytes, payload_size: int) -> dict:
    expected_size = HEADER_SIZE + payload_size

    if len(packet) != expected_size:
        raise ValueError("Wrong packet size.")

    (
        magic,
        sequence,
        timestamp_ms,
        payload_length,
        flags,
        expected_crc,
    ) = struct.unpack(
        HEADER_FORMAT,
        packet[:HEADER_SIZE],
    )

    if magic != MAGIC:
        raise ValueError("Wrong packet header.")

    if payload_length > payload_size:
        raise ValueError("Wrong payload length.")

    payload = packet[
        HEADER_SIZE:
        HEADER_SIZE + payload_length
    ]

    received_crc = zlib.crc32(payload) & 0xFFFFFFFF

    if received_crc != expected_crc:
        raise ValueError("CRC failed.")

    return {
        "sequence": sequence,
        "timestamp_ms": timestamp_ms,
        "flags": flags,
        "payload": payload,
    }


# ------------------------------------------------------------
# BPSK waveform
# ------------------------------------------------------------

def packet_to_iq(
    packet: bytes,
    samples_per_bit: int,
    scale: float = 2**13,
) -> np.ndarray:
    packet_bits = bytes_to_bits(packet)

    frame_bits = np.concatenate(
        (PREAMBLE_BITS, packet_bits)
    )

    symbols = bits_to_bpsk(frame_bits)
    samples = np.repeat(symbols, samples_per_bit)

    # Short zero guards separate cyclic repetitions.
    guard = np.zeros(
        16 * samples_per_bit,
        dtype=np.complex64,
    )

    return (
        np.concatenate((guard, samples, guard))
        * scale
    ).astype(np.complex64)


def radio_frame_samples(
    payload_size: int,
    samples_per_bit: int,
) -> int:
    packet_bytes = HEADER_SIZE + payload_size
    frame_bits = len(PREAMBLE_BITS) + packet_bytes * 8

    return (
        frame_bits * samples_per_bit
        + 32 * samples_per_bit
    )


# ------------------------------------------------------------
# Receiver synchronization and BPSK decisions
# ------------------------------------------------------------

def recover_packet(
    rx_samples: np.ndarray,
    payload_size: int,
    samples_per_bit: int,
    expected_sequence: int,
    candidates_per_phase: int = 16,
    preferred_phase: int | None = None,
    fast_candidates: int = 4,
) -> tuple[dict, float, int]:
    """
    Recover only the packet currently expected by the receiver.

    Fast path:
    - Search the previously successful sample phase first.
    - Test only a small number of candidates on that phase.
    - Fall back to the full four-phase search when needed.

    The synchronization search uses:
        preamble + magic word + expected sequence number
    """
    rx_samples = np.asarray(
        rx_samples,
        dtype=np.complex64,
    )

    rx_samples = rx_samples - np.mean(rx_samples)

    packet_bytes = HEADER_SIZE + payload_size
    complete_frame_bits = (
        len(PREAMBLE_BITS) + packet_bytes * 8
    )

    sequence_bytes = struct.pack("!I", expected_sequence)
    sequence_bits = bytes_to_bits(sequence_bytes)

    expected_sync_bits = np.concatenate(
        (PREAMBLE_BITS, MAGIC_BITS, sequence_bits)
    )
    sync_symbols = bits_to_bpsk(expected_sync_bits)
    sync_energy = float(
        np.sum(np.abs(sync_symbols) ** 2)
    )
    energy_kernel = np.ones(len(sync_symbols))

    phase_cache: dict[
        int,
        tuple[np.ndarray, np.ndarray] | None,
    ] = {}

    def prepare_phase(
        phase: int,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        if phase in phase_cache:
            return phase_cache[phase]

        usable = (
            (len(rx_samples) - phase)
            // samples_per_bit
            * samples_per_bit
        )

        if usable <= 0:
            phase_cache[phase] = None
            return None

        blocks = rx_samples[
            phase:
            phase + usable
        ].reshape(-1, samples_per_bit)

        edge = max(1, samples_per_bit // 8)

        if samples_per_bit - 2 * edge > 0:
            symbol_stream = np.mean(
                blocks[:, edge:-edge],
                axis=1,
            )
        else:
            symbol_stream = np.mean(blocks, axis=1)

        if len(symbol_stream) < complete_frame_bits:
            phase_cache[phase] = None
            return None

        correlation = np.abs(
            np.correlate(
                symbol_stream,
                sync_symbols,
                mode="valid",
            )
        )

        window_energy = np.convolve(
            np.abs(symbol_stream) ** 2,
            energy_kernel,
            mode="valid",
        )

        metric = correlation / np.sqrt(
            window_energy * sync_energy + 1e-12
        )

        last_valid_start = (
            len(symbol_stream) - complete_frame_bits
        )
        metric = metric[:last_valid_start + 1]

        if len(metric) == 0:
            phase_cache[phase] = None
            return None

        prepared = (symbol_stream, metric)
        phase_cache[phase] = prepared
        return prepared

    def collect_candidates(
        phases: list[int],
        count_per_phase: int,
    ) -> list[tuple[float, int, np.ndarray, int]]:
        candidates = []

        for phase in phases:
            prepared = prepare_phase(phase)

            if prepared is None:
                continue

            symbol_stream, metric = prepared
            count = min(
                max(1, count_per_phase),
                len(metric),
            )
            indexes = np.argpartition(
                metric,
                -count,
            )[-count:]

            for index in indexes:
                candidates.append(
                    (
                        float(metric[index]),
                        int(index),
                        symbol_stream,
                        phase,
                    )
                )

        candidates.sort(
            key=lambda item: item[0],
            reverse=True,
        )
        return candidates

    def decode_candidates(
        candidates: list[
            tuple[float, int, np.ndarray, int]
        ],
    ) -> tuple[dict, float, int] | None:
        nonlocal last_error

        for metric, start_symbol, symbol_stream, phase in candidates:
            try:
                received_frame = symbol_stream[
                    start_symbol:
                    start_symbol + complete_frame_bits
                ]

                received_sync = received_frame[
                    :len(expected_sync_bits)
                ]

                channel = (
                    np.vdot(sync_symbols, received_sync)
                    / np.vdot(sync_symbols, sync_symbols)
                )

                if abs(channel) < 1e-6:
                    raise ValueError("Weak channel estimate.")

                corrected = received_frame / channel

                recovered_bits = (
                    np.real(corrected) < 0
                ).astype(np.uint8)

                packet_bits = recovered_bits[
                    len(PREAMBLE_BITS):
                ]

                packet = bits_to_bytes(packet_bits)
                fields = parse_packet(packet, payload_size)

                if fields["sequence"] != expected_sequence:
                    raise ValueError(
                        f"Expected packet {expected_sequence}, "
                        f"received {fields['sequence']}"
                    )

                return fields, metric, phase

            except Exception as error:
                last_error = error

        return None

    last_error: Exception | None = None
    all_phases = list(range(samples_per_bit))

    # Fast path: previous successful phase only.
    if (
        preferred_phase is not None
        and 0 <= preferred_phase < samples_per_bit
    ):
        result = decode_candidates(
            collect_candidates(
                [preferred_phase],
                min(
                    max(1, fast_candidates),
                    candidates_per_phase,
                ),
            )
        )

        if result is not None:
            return result

    # Full fallback. The phase cache prevents recomputing a phase that the
    # fast path already prepared.
    result = decode_candidates(
        collect_candidates(
            all_phases,
            candidates_per_phase,
        )
    )

    if result is not None:
        return result

    if all(
        prepare_phase(phase) is None
        for phase in all_phases
    ):
        raise ValueError("No complete SDR frame found.")

    raise ValueError(f"No valid packet found: {last_error}")


# ------------------------------------------------------------
# FFmpeg camera sources and FFplay
# ------------------------------------------------------------

def encoder_key_interval(args) -> int:
    """Return the selected HEVC keyframe interval in frames."""
    return max(
        1,
        int(round(args.fps * args.keyint_seconds)),
    )


def low_bitrate_x265_params(args) -> str:
    """
    Build low-overhead HEVC settings for the slow SDR link.

    A longer GOP avoids repeating VPS/SPS/PPS headers every second. AUD NAL
    units are disabled because FFplay does not require them for this pipe.
    """
    key_interval = encoder_key_interval(args)
    vbv_buffer = max(args.video_bitrate * 4, 4)

    return (
        "repeat-headers=1:"
        "aud=0:"
        f"keyint={key_interval}:"
        f"min-keyint={key_interval}:"
        "scenecut=0:"
        "bframes=0:"
        f"vbv-maxrate={args.video_bitrate}:"
        f"vbv-bufsize={vbv_buffer}"
    )


def start_generated_camera(args):
    """Generate FFmpeg's colored testsrc2 diagnostic pattern."""

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-re",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={args.video_size}:rate={args.fps}",
        "-t",
        str(args.duration),
        "-an",
        "-c:v",
        "libx265",
        "-preset",
        args.encoder_preset,
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        f"{args.video_bitrate}k",
        "-maxrate",
        f"{args.video_bitrate}k",
        "-bufsize",
        f"{max(args.video_bitrate * 4, 4)}k",
        "-x265-params",
        low_bitrate_x265_params(args),
        "-flush_packets",
        "1",
        "-f",
        "hevc",
        "pipe:1",
    ]

    print("Starting FFmpeg testsrc2 diagnostic pattern.")
    print(
        "This mode intentionally shows moving color bars and does not use "
        "the supplied camera/file video."
    )

    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        bufsize=0,
    )


class DirectFileCamera:
    """Expose a normal HEVC file through the same .stdout interface."""

    def __init__(self, path: Path):
        self.stdout = path.open("rb")

    def poll(self):
        return None if not self.stdout.closed else 0

    def terminate(self):
        if not self.stdout.closed:
            self.stdout.close()


class LoopingRawHevcCamera:
    """
    Feed a raw HEVC elementary stream repeatedly into FFmpeg through stdin.

    FFmpeg's -stream_loop option is unreliable for raw HEVC because the raw
    demuxer may not support seeking back to the beginning. This class performs
    the looping in Python, so FFmpeg sees one continuous elementary stream.
    """

    def __init__(
        self,
        command: list[str],
        path: Path,
        read_size: int = 64 * 1024,
    ):
        self.path = path
        self.read_size = max(4096, int(read_size))
        self._stop_event = threading.Event()
        self._feed_error: Exception | None = None

        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )

        if self.process.stdin is None:
            self.process.terminate()
            raise RuntimeError("Could not open FFmpeg stdin.")

        if self.process.stdout is None:
            self.process.terminate()
            raise RuntimeError("Could not open FFmpeg stdout.")

        self.stdout = self.process.stdout

        self._thread = threading.Thread(
            target=self._feed_loop,
            name="raw-hevc-loop-feeder",
            daemon=True,
        )
        self._thread.start()

    def _feed_loop(self) -> None:
        try:
            assert self.process.stdin is not None

            while (
                not self._stop_event.is_set()
                and self.process.poll() is None
            ):
                with self.path.open("rb") as source:
                    while not self._stop_event.is_set():
                        chunk = source.read(self.read_size)

                        if not chunk:
                            break

                        self.process.stdin.write(chunk)

        except (BrokenPipeError, OSError) as error:
            # A broken pipe is normal when FFmpeg reaches -t duration.
            if self.process.poll() is None:
                self._feed_error = error

        except Exception as error:
            self._feed_error = error

        finally:
            if self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except Exception:
                    pass

    def poll(self):
        return self.process.poll()

    def terminate(self):
        self._stop_event.set()

        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except Exception:
                pass

        if self.process.poll() is None:
            self.process.terminate()

        self._thread.join(timeout=1.0)

    @property
    def feed_error(self) -> Exception | None:
        return self._feed_error


def probe_hevc_frame_count(
    path: Path,
    frame_rate: int,
) -> int | None:
    """Count decoded frames in a raw HEVC file using ffprobe."""
    if not path.exists() or path.stat().st_size == 0:
        return None

    command = [
        "ffprobe",
        "-v",
        "error",
        "-f",
        "hevc",
        "-framerate",
        str(frame_rate),
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    output = result.stdout.strip()

    try:
        return int(output)
    except ValueError:
        return None



class BufferedLivePipeCamera:
    """
    Read FFmpeg's live HEVC output in background threads.

    Important fixes:
    - os.read() returns currently available pipe bytes instead of waiting for a
      large Python buffered read;
    - FFmpeg stderr is drained continuously, preventing stderr pipe blockage;
    - startup and idle timeouts prevent silent infinite waits;
    - recent FFmpeg errors are retained for useful diagnostics.
    """

    def __init__(
        self,
        command: list[str],
        max_buffer_bytes: int,
        read_size: int = 1024,
        startup_timeout_seconds: float = 15.0,
        idle_timeout_seconds: float = 20.0,
    ):
        self.max_buffer_bytes = max(4096, int(max_buffer_bytes))
        self.read_size = min(
            4096,
            max(256, int(read_size)),
        )
        self.startup_timeout_seconds = max(
            1.0,
            float(startup_timeout_seconds),
        )
        self.idle_timeout_seconds = max(
            1.0,
            float(idle_timeout_seconds),
        )

        self._buffer = bytearray()
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._reader_finished = False
        self._reader_error: Exception | None = None
        self._high_water_bytes = 0
        self._total_bytes_read = 0
        self._started_at = time.monotonic()
        self._last_output_at = self._started_at
        self._stderr_lines: deque[str] = deque(maxlen=30)

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        if self.process.stdout is None:
            self.process.terminate()
            raise RuntimeError("Could not open FFmpeg RTSP stdout.")

        if self.process.stderr is None:
            self.process.terminate()
            raise RuntimeError("Could not open FFmpeg RTSP stderr.")

        # Compatibility with read_stream_chunk(camera.stdout, ...).
        self.stdout = self

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="rtsp-hevc-stdout-reader",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name="rtsp-ffmpeg-stderr-reader",
            daemon=True,
        )

        self._reader_thread.start()
        self._stderr_thread.start()

    @staticmethod
    def _redact_credentials(message: str) -> str:
        """Hide an RTSP password if FFmpeg includes the URL in an error."""
        return re.sub(
            r"(rtsp://[^:/@\s]+:)[^@/\s]+(@)",
            r"\1***\2",
            message,
        )

    def _stderr_loop(self) -> None:
        assert self.process.stderr is not None

        try:
            while not self._stop_event.is_set():
                # Drain stderr until EOF even if FFmpeg has already exited.
                raw_line = self.process.stderr.readline()

                if not raw_line:
                    break

                line = raw_line.decode(
                    "utf-8",
                    errors="replace",
                ).rstrip()

                if not line:
                    continue

                line = self._redact_credentials(line)
                self._stderr_lines.append(line)

                # loglevel=warning means these lines are useful and limited.
                print(f"[FFmpeg] {line}", flush=True)

        except Exception as error:
            self._stderr_lines.append(
                f"stderr reader error: {error}"
            )

    def _reader_loop(self) -> None:
        try:
            assert self.process.stdout is not None
            output_fd = self.process.stdout.fileno()

            while not self._stop_event.is_set():
                # Drain stdout until EOF even if FFmpeg exits after writing a
                # short final block.
                # A raw POSIX pipe read returns as soon as bytes are available.
                # It does not wait for the complete requested block.
                chunk = os.read(output_fd, self.read_size)

                if not chunk:
                    break

                with self._condition:
                    while (
                        len(self._buffer) + len(chunk)
                        > self.max_buffer_bytes
                        and not self._stop_event.is_set()
                    ):
                        self._condition.wait(timeout=0.1)

                    if self._stop_event.is_set():
                        break

                    self._buffer.extend(chunk)
                    self._total_bytes_read += len(chunk)
                    self._last_output_at = time.monotonic()
                    self._high_water_bytes = max(
                        self._high_water_bytes,
                        len(self._buffer),
                    )
                    self._condition.notify_all()

        except Exception as error:
            if not self._stop_event.is_set():
                self._reader_error = error

        finally:
            with self._condition:
                self._reader_finished = True
                self._condition.notify_all()

    def _timeout_message(self, startup: bool) -> str:
        mode = "startup" if startup else "idle"
        recent = self.recent_stderr

        message = (
            f"FFmpeg RTSP {mode} timeout: no encoded HEVC bytes "
            f"arrived within "
            f"{self.startup_timeout_seconds if startup else self.idle_timeout_seconds:.1f} "
            "seconds."
        )

        if recent:
            message += " Recent FFmpeg output: " + " | ".join(
                recent[-5:]
            )

        return message

    def read(self, size: int) -> bytes:
        """
        Return up to ``size`` buffered HEVC bytes.

        The initial read has a finite startup timeout. Later reads have an idle
        timeout so a disconnected camera cannot freeze the SDR application.
        """
        if size <= 0:
            return b""

        with self._condition:
            while (
                len(self._buffer) < size
                and not self._reader_finished
                and not self._stop_event.is_set()
            ):
                now = time.monotonic()
                startup = self._total_bytes_read == 0

                if startup:
                    elapsed = now - self._started_at
                    remaining = (
                        self.startup_timeout_seconds - elapsed
                    )
                else:
                    elapsed = now - self._last_output_at
                    remaining = (
                        self.idle_timeout_seconds - elapsed
                    )

                if remaining <= 0:
                    self._reader_error = TimeoutError(
                        self._timeout_message(startup)
                    )
                    self._reader_finished = True
                    self._condition.notify_all()
                    break

                self._condition.wait(
                    timeout=min(0.1, remaining)
                )

            if not self._buffer:
                return b""

            count = min(size, len(self._buffer))
            data = bytes(self._buffer[:count])
            del self._buffer[:count]
            self._condition.notify_all()
            return data

    def poll(self):
        return self.process.poll()

    def terminate(self):
        self._stop_event.set()

        with self._condition:
            self._condition.notify_all()

        if self.process.poll() is None:
            self.process.terminate()

        self._reader_thread.join(timeout=2.0)
        self._stderr_thread.join(timeout=2.0)

        if self.process.poll() is None:
            self.process.kill()

    @property
    def buffered_bytes(self) -> int:
        with self._condition:
            return len(self._buffer)

    @property
    def high_water_bytes(self) -> int:
        return self._high_water_bytes

    @property
    def total_bytes_read(self) -> int:
        return self._total_bytes_read

    @property
    def reader_error(self) -> Exception | None:
        return self._reader_error

    @property
    def recent_stderr(self) -> list[str]:
        return list(self._stderr_lines)


def start_rtsp_camera(args):
    """
    Connect to a real RTSP camera and transcode it to low-bitrate raw HEVC.

    The camera URL is resolved from --camera-url or from the environment
    variable named by --camera-url-env. The URL itself is intentionally not
    printed, preventing credentials from appearing in normal logs.
    """
    camera_url = args.camera_url or CAMERA_URL

    if not camera_url or "PASSWORD" in camera_url:
        raise ValueError(
            "Edit CAMERA_URL near the top of this Python file and replace "
            "PASSWORD with the local camera password."
        )

    try:
        width_text, height_text = args.video_size.lower().split("x", 1)
        output_width = int(width_text)
        output_height = int(height_text)
    except (ValueError, AttributeError) as error:
        raise ValueError(
            "--video-size must use WIDTHxHEIGHT, for example 256x144."
        ) from error

    if output_width < 16 or output_height < 16:
        raise ValueError("--video-size dimensions must be at least 16 pixels.")

    video_filter = (
        f"scale={output_width}:{output_height}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={output_width}:{output_height}:"
        "(ow-iw)/2:(oh-ih)/2,"
        f"fps={args.fps},format=yuv420p"
    )

    # Keep the RTSP input options deliberately minimal. This mirrors the
    # standalone FFmpeg command already proven to work with the camera and is
    # compatible with older FFmpeg packages.
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        args.rtsp_transport,
        "-i",
        camera_url,
    ]

    if args.duration > 0:
        command.extend(["-t", str(args.duration)])

    command.extend(
        [
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            video_filter,
            "-c:v",
            "libx265",
            "-preset",
            args.encoder_preset,
            "-tune",
            "zerolatency",
            "-b:v",
            f"{args.video_bitrate}k",
            "-maxrate",
            f"{args.video_bitrate}k",
            "-bufsize",
            f"{max(args.video_bitrate * 4, 4)}k",
            "-x265-params",
            low_bitrate_x265_params(args),
            "-flush_packets",
            "1",
            "-f",
            "hevc",
            "pipe:1",
        ]
    )

    print("Starting real RTSP camera source.")
    print(
        f"RTSP transport:      {args.rtsp_transport}; "
        "camera URL loaded from the local CAMERA_URL constant; "
        "minimal legacy-compatible FFmpeg input options."
    )
    print(
        f"Transcoding as {args.video_size}, {args.fps} fps, "
        f"target {args.video_bitrate} kbit/s."
    )
    print(
        f"Camera/SDR queue:    {args.camera_buffer_bytes:,} bytes "
        f"(delay buffer)."
    )

    return BufferedLivePipeCamera(
        command=command,
        max_buffer_bytes=args.camera_buffer_bytes,
        read_size=args.camera_read_size,
        startup_timeout_seconds=args.rtsp_timeout_seconds,
        idle_timeout_seconds=max(
            args.rtsp_timeout_seconds * 2.0,
            15.0,
        ),
    )



def next_power_of_two(value: int) -> int:
    """Return a Pluto-friendly power-of-two RX buffer size."""
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def start_file_camera(args):
    """
    Start a raw HEVC file source.

    Default mode re-encodes the supplied video to the selected low bitrate.

    With --loop-input, Python repeatedly feeds the raw HEVC bytes into FFmpeg.
    This avoids FFmpeg's unreliable seeking of raw HEVC elementary streams.

    With --copy-original, the program reads the original elementary stream once
    and copies its bytes unchanged.
    """

    if args.input is None:
        raise ValueError("--input is required for file mode.")

    if args.copy_original:
        print("Starting direct original HEVC file source.")
        print(
            "Reading the exact HEVC bytes once without FFmpeg, looping, "
            "resizing or re-encoding."
        )
        return DirectFileCamera(args.input)

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-re",
        "-f",
        "hevc",
        "-framerate",
        str(args.input_fps),
        "-i",
        "pipe:0" if args.loop_input else str(args.input),
        "-t",
        str(args.duration),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        f"scale={args.video_size},fps={args.fps},format=yuv420p",
        "-c:v",
        "libx265",
        "-preset",
        args.encoder_preset,
        "-tune",
        "zerolatency",
        "-b:v",
        f"{args.video_bitrate}k",
        "-maxrate",
        f"{args.video_bitrate}k",
        "-bufsize",
        f"{max(args.video_bitrate * 4, 4)}k",
        "-x265-params",
        low_bitrate_x265_params(args),
        "-flush_packets",
        "1",
        "-f",
        "hevc",
        "pipe:1",
    ]

    print("Starting the supplied HEVC file as the video source.")
    print(
        f"Re-encoding the real input video as {args.video_size}, "
        f"{args.fps} fps, target {args.video_bitrate} kbit/s, "
        f"keyframe every {args.keyint_seconds:g} seconds."
    )

    if args.loop_input:
        print(
            "Looping method: Python raw-HEVC feeder "
            "(no FFmpeg seek required)."
        )
        return LoopingRawHevcCamera(
            command=command,
            path=args.input,
        )

    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        bufsize=0,
    )


def start_player():
    command = [
        "ffplay",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-probesize",
        "2000000",
        "-analyzeduration",
        "2000000",
        "-f",
        "hevc",
        "-i",
        "pipe:0",
    ]

    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        bufsize=0,
    )



def read_stream_chunk(stream, size: int) -> bytes:
    """
    Read up to exactly `size` bytes from a live FFmpeg pipe.

    A pipe can return a short read even when the stream has not ended.
    This function joins those short reads into one complete SDR payload.
    """
    data = bytearray()

    while len(data) < size:
        block = stream.read(size - len(data))

        if not block:
            break

        data.extend(block)

    return bytes(data)


# ------------------------------------------------------------
# Main live-stream loop
# ------------------------------------------------------------


def open_pluto(args, rx_buffer_size: int):
    """Create and configure one Pluto object."""
    if adi is None:
        raise RuntimeError("pyadi-iio is not installed.")

    device = adi.Pluto(args.uri)
    device.sample_rate = int(args.sample_rate)

    device.tx_lo = int(args.frequency)
    device.tx_rf_bandwidth = int(args.sample_rate)
    device.tx_hardwaregain_chan0 = float(args.tx_gain)

    device.rx_lo = int(args.frequency)
    device.rx_rf_bandwidth = int(args.sample_rate)
    device.rx_buffer_size = int(rx_buffer_size)
    device.gain_control_mode_chan0 = "manual"
    device.rx_hardwaregain_chan0 = float(args.rx_gain)

    return device


def release_pluto(device) -> None:
    """Release host TX/RX buffers before dropping the libiio context."""
    if device is None:
        return

    try:
        device.tx_destroy_buffer()
    except Exception:
        pass

    if hasattr(device, "rx_destroy_buffer"):
        try:
            device.rx_destroy_buffer()
        except Exception:
            pass



def refresh_rx_buffer(
    device,
    pause: float = 0.02,
) -> None:
    """
    Recreate only the host RX buffer while keeping cyclic TX active.

    This is cheaper than destroying and retransmitting the current packet.
    It helps when libiio/WSL returns stale or poorly aligned RX data.
    """
    if device is None:
        return

    if hasattr(device, "rx_destroy_buffer"):
        try:
            device.rx_destroy_buffer()
        except Exception:
            pass

    if pause > 0:
        time.sleep(pause)



def inplace_rx_resync(
    device,
    args,
    rx_buffer_size: int,
    reason: str,
) -> None:
    """
    Perform a genuinely soft RX-only resynchronization.

    The current cyclic TX waveform remains active. Only the RX host buffer and
    RX-side settings are refreshed. This avoids the costly TX destruction,
    radio-wide reconfiguration and retransmission used by the older soft reset.
    """
    if device is None:
        raise RuntimeError("Cannot resynchronize an unavailable Pluto.")

    print(f"RX RESYNC (soft): {reason}")

    if hasattr(device, "rx_destroy_buffer"):
        try:
            device.rx_destroy_buffer()
        except Exception:
            pass

    # Reapply only RX-side settings.
    device.rx_lo = int(args.frequency)
    device.rx_rf_bandwidth = int(args.sample_rate)
    device.gain_control_mode_chan0 = "manual"
    device.rx_hardwaregain_chan0 = float(args.rx_gain)
    device.rx_buffer_size = int(rx_buffer_size)

    if args.reset_pause > 0:
        time.sleep(min(args.reset_pause, 0.05))

    print("RX RESYNC: cyclic TX remains active.")


def inplace_radio_resync(
    device,
    args,
    rx_buffer_size: int,
    reason: str,
    stronger: bool = False,
):
    """
    Resynchronize the existing Pluto object without reopening its USB context.

    Reopening adi.Pluto inside the same WSL process can fail because libiio/
    usbipd may still hold the USB interface. This reset therefore:
      - destroys host TX/RX buffers;
      - reapplies sample rate, LO, bandwidth and gain;
      - recreates the RX buffer configuration;
      - pauses briefly before the same packet is retried.
    """
    if device is None:
        raise RuntimeError("Cannot resynchronize an unavailable Pluto.")

    level = "strong" if stronger else "soft"
    print(f"RADIO RESYNC ({level}): {reason}")

    try:
        device.tx_destroy_buffer()
    except Exception:
        pass

    if hasattr(device, "rx_destroy_buffer"):
        try:
            device.rx_destroy_buffer()
        except Exception:
            pass

    # Reapply all settings to the existing libiio context.
    device.sample_rate = int(args.sample_rate)

    device.tx_lo = int(args.frequency)
    device.tx_rf_bandwidth = int(args.sample_rate)
    device.tx_hardwaregain_chan0 = float(args.tx_gain)

    device.rx_lo = int(args.frequency)
    device.rx_rf_bandwidth = int(args.sample_rate)
    device.gain_control_mode_chan0 = "manual"
    device.rx_hardwaregain_chan0 = float(args.rx_gain)
    device.rx_buffer_size = int(rx_buffer_size)

    # A stronger reset uses a longer settling pause.
    pause = args.reset_pause * (2.0 if stronger else 1.0)
    if pause > 0:
        time.sleep(pause)

    print("RADIO RESYNC: existing Pluto context is ready.")
    return device


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "One-Pluto H.265 transmission with balanced padding, persistent cyclic TX across retries, RX-only soft resynchronization, phase-tracked synchronization and detailed timing."
        )
    )

    parser.add_argument(
        "--source",
        choices=("generated", "file", "rtsp"),
        default="generated",
    )

    parser.add_argument(
        "--input",
        type=Path,
        help="Raw H.265 file for --source file.",
    )


    parser.add_argument(
        "--camera-url",
        help=(
            "Optional RTSP URL override. Normally edit CAMERA_URL near the "
            "top of this local Python file."
        ),
    )

    parser.add_argument(
        "--camera-url-env",
        default="CAMERA_URL",
        help=(
            "Deprecated compatibility option. V8.8 normally uses the "
            "CAMERA_URL constant in this Python file."
        ),
    )

    parser.add_argument(
        "--rtsp-transport",
        choices=("tcp", "udp"),
        default="tcp",
        help="RTSP transport. TCP is normally more reliable for this project.",
    )

    parser.add_argument(
        "--rtsp-timeout-seconds",
        type=float,
        default=10.0,
        help="RTSP socket read/write timeout.",
    )

    parser.add_argument(
        "--rtsp-thread-queue",
        type=int,
        default=512,
        help="FFmpeg input queue size for the live RTSP stream.",
    )

    parser.add_argument(
        "--camera-buffer-bytes",
        type=int,
        default=256 * 1024,
        help=(
            "Maximum encoded HEVC bytes buffered between FFmpeg and the SDR "
            "loop. Larger values permit more delay and absorb more retries."
        ),
    )

    parser.add_argument(
        "--camera-read-size",
        type=int,
        default=1024,
        help=(
            "Raw FFmpeg stdout read size. Values from 512 to 4096 are "
            "appropriate for low-bitrate HEVC."
        ),
    )

    parser.add_argument(
        "--input-fps",
        type=int,
        default=30,
        help="Frame rate of the original raw H.265 input.",
    )

    parser.add_argument(
        "--copy-original",
        action="store_true",
        help=(
            "Transmit the original raw HEVC bytes once without resizing, "
            "frame-rate conversion, bitrate conversion or looping."
        ),
    )

    parser.add_argument(
        "--loop-input",
        action="store_true",
        help=(
            "Loop the file only in re-encode mode. Leave disabled for raw "
            "HEVC files that report 'Seek to start failed'."
        ),
    )

    parser.add_argument(
        "--tx-save",
        type=Path,
        default=Path("transmitted_camera.h265"),
    )

    parser.add_argument(
        "--rx-save",
        type=Path,
        default=Path("received_camera.h265"),
    )

    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--video-size", default="160x96")
    parser.add_argument("--fps", type=int, default=5)

    parser.add_argument(
        "--video-bitrate",
        type=int,
        default=4,
        help="Generated H.265 bitrate in kbit/s.",
    )

    parser.add_argument(
        "--keyint-seconds",
        type=float,
        default=5.0,
        help=(
            "HEVC keyframe interval for generated/re-encoded streaming. "
            "A longer interval reduces repeated-header overhead."
        ),
    )

    parser.add_argument(
        "--encoder-preset",
        choices=(
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
        ),
        default="veryfast",
        help=(
            "libx265 speed/efficiency preset. 'veryfast' is a practical "
            "low-resolution streaming compromise."
        ),
    )

    parser.add_argument("--uri", default="usb:")
    parser.add_argument("--frequency", type=int, default=915_000_000)
    parser.add_argument("--sample-rate", type=int, default=4_000_000)
    parser.add_argument("--samples-per-bit", type=int, default=2)
    parser.add_argument("--payload-size", type=int, default=2048)
    parser.add_argument("--tx-gain", type=float, default=-50)
    parser.add_argument("--rx-gain", type=float, default=0)
    parser.add_argument("--retries", type=int, default=10)
    parser.add_argument(
        "--captures-per-attempt",
        type=int,
        default=2,
        help="RX buffers searched for each transmitted packet.",
    )

    parser.add_argument(
        "--fresh-rx-captures",
        type=int,
        default=2,
        help=(
            "Extra RX buffers searched after an RX-only buffer refresh while "
            "the same cyclic TX packet remains active. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--fresh-rx-pause",
        type=float,
        default=0.02,
        help="Pause after an RX-only buffer refresh, in seconds.",
    )
    parser.add_argument(
        "--startup-extra-hold",
        type=float,
        default=0.05,
        help=(
            "Additional TX settling time for packet 0 only, in seconds."
        ),
    )
    parser.add_argument(
        "--rx-warmup-captures",
        type=int,
        default=1,
        help=(
            "RX buffers discarded after TX starts, before synchronization "
            "search begins."
        ),
    )
    parser.add_argument(
        "--sync-candidates",
        type=int,
        default=16,
        help="Correlation candidates tested per sample phase.",
    )

    parser.add_argument(
        "--fast-sync-candidates",
        type=int,
        default=4,
        help=(
            "Candidates tested on the previously successful sample phase "
            "before the full four-phase fallback."
        ),
    )
    parser.add_argument(
        "--disable-phase-tracking",
        action="store_true",
        help=(
            "Disable the previous-phase fast synchronization path."
        ),
    )
    parser.add_argument(
        "--timing-report-every",
        type=int,
        default=0,
        help=(
            "Print cumulative stage timing every N recovered packets. "
            "Use 0 to print only the final timing report."
        ),
    )
    parser.add_argument(
        "--playback-prebuffer-bytes",
        type=int,
        default=0,
        help=(
            "Buffer this many received HEVC bytes before starting FFplay. "
            "Adds delay but reduces playback stalls. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--tx-hold-frames",
        type=float,
        default=1.2,
        help=(
            "Complete cyclic radio frames transmitted before the first RX read."
        ),
    )
    parser.add_argument(
        "--minimum-tx-hold",
        type=float,
        default=0.004,
        help="Minimum cyclic-TX settling time before the first RX read.",
    )
    parser.add_argument(
        "--rx-buffer-frames",
        type=float,
        default=2.0,
        help="RX buffer length measured in complete radio frames.",
    )
    parser.add_argument(
        "--flush-rx-every-packet",
        action="store_true",
        help="Safer but slower: recreate the host RX buffer for each packet.",
    )

    parser.add_argument(
        "--soft-reset-after",
        type=int,
        default=3,
        help=(
            "Recreate the RX buffer after this many consecutive failed "
            "attempts for the same packet. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--hard-reset-after",
        type=int,
        default=8,
        help=(
            "Perform a stronger in-place buffer/configuration resync after "
            "this many consecutive failed attempts. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--periodic-reset-packets",
        type=int,
        default=48,
        help=(
            "Proactively resynchronize buffers on the existing Pluto context "
            "after this many successfully received packets. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--reset-pause",
        type=float,
        default=0.15,
        help="Pause in seconds after an in-place radio resynchronization.",
    )
    parser.add_argument(
        "--reopen-attempts",
        type=int,
        default=3,
        help=(
            "Retained for v7 command compatibility; v8 does not reopen the "
            "USB context."
        ),
    )

    parser.add_argument(
        "--no-display",
        action="store_true",
    )

    parser.add_argument(
        "--software-only",
        action="store_true",
        help="Test FFmpeg/FFplay without Pluto.",
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="Stop if one SDR packet cannot be recovered.",
    )

    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg was not found in WSL.")

    if not args.software_only and adi is None:
        raise RuntimeError(
            "pyadi-iio is not installed. Install it or use --software-only."
        )

    if not args.no_display and shutil.which("ffplay") is None:
        raise RuntimeError("FFplay was not found in WSL.")

    if args.source == "file" and args.input is None:
        parser.error("--input is required with --source file")

    if args.source == "rtsp":
        selected_camera_url = args.camera_url or CAMERA_URL

        if not selected_camera_url or "PASSWORD" in selected_camera_url:
            parser.error(
                "Edit CAMERA_URL near the top of this Python file and "
                "replace PASSWORD with the local camera password"
            )

    if args.copy_original and args.source != "file":
        parser.error("--copy-original can only be used with --source file")

    if args.duration < 0:
        parser.error("--duration must be zero or greater")
    if args.rtsp_timeout_seconds <= 0:
        parser.error("--rtsp-timeout-seconds must be greater than zero")
    if args.rtsp_thread_queue < 1:
        parser.error("--rtsp-thread-queue must be at least 1")
    if args.camera_buffer_bytes < args.payload_size * 2:
        parser.error(
            "--camera-buffer-bytes must hold at least two SDR payloads"
        )
    if args.camera_read_size < 256:
        parser.error("--camera-read-size must be at least 256 bytes")

    if args.video_bitrate < 1:
        parser.error("--video-bitrate must be at least 1")
    if args.keyint_seconds <= 0:
        parser.error("--keyint-seconds must be greater than zero")

    if args.samples_per_bit < 1:
        parser.error("--samples-per-bit must be at least 1")
    if args.payload_size < 64:
        parser.error("--payload-size must be at least 64 bytes")
    if args.captures_per_attempt < 1:
        parser.error("--captures-per-attempt must be at least 1")
    if args.fresh_rx_captures < 0:
        parser.error("--fresh-rx-captures must be zero or greater")
    if args.fresh_rx_pause < 0:
        parser.error("--fresh-rx-pause must be zero or greater")
    if args.startup_extra_hold < 0:
        parser.error("--startup-extra-hold must be zero or greater")
    if args.rx_warmup_captures < 0:
        parser.error("--rx-warmup-captures must be zero or greater")
    if args.sync_candidates < 1:
        parser.error("--sync-candidates must be at least 1")
    if args.fast_sync_candidates < 1:
        parser.error("--fast-sync-candidates must be at least 1")
    if args.timing_report_every < 0:
        parser.error("--timing-report-every must be zero or greater")
    if args.playback_prebuffer_bytes < 0:
        parser.error("--playback-prebuffer-bytes must be zero or greater")
    if args.tx_hold_frames < 1.0:
        parser.error("--tx-hold-frames must be at least 1.0")
    if args.rx_buffer_frames < 1.25:
        parser.error("--rx-buffer-frames should be at least 1.25")
    if args.soft_reset_after < 0:
        parser.error("--soft-reset-after must be zero or greater")
    if args.hard_reset_after < 0:
        parser.error("--hard-reset-after must be zero or greater")
    if args.periodic_reset_packets < 0:
        parser.error("--periodic-reset-packets must be zero or greater")
    if args.reset_pause < 0:
        parser.error("--reset-pause must be zero or greater")
    if args.reopen_attempts < 1:
        parser.error("--reopen-attempts must be at least 1")
    if (
        args.soft_reset_after > 0
        and args.hard_reset_after > 0
        and args.soft_reset_after >= args.hard_reset_after
    ):
        parser.error(
            "--soft-reset-after must be lower than --hard-reset-after"
        )

    args.tx_save.parent.mkdir(parents=True, exist_ok=True)
    args.rx_save.parent.mkdir(parents=True, exist_ok=True)

    if args.source == "generated":
        camera = start_generated_camera(args)
    elif args.source == "file":
        camera = start_file_camera(args)
    else:
        camera = start_rtsp_camera(args)

    if camera.stdout is None:
        raise RuntimeError("Could not read FFmpeg output.")

    player = None
    playback_pending = bytearray()

    if (
        not args.no_display
        and args.playback_prebuffer_bytes == 0
    ):
        player = start_player()

    gross_bps = args.sample_rate / args.samples_per_bit

    print("\n========== LIVE CAMERA SIMULATION ==========")
    print(f"Source:              {args.source}")

    if args.source == "file" and args.copy_original:
        print("Mode:                original HEVC stream copy")
        print(f"Input frame rate:    {args.input_fps} fps")
        print("Duration:            until the original file ends")
        print("Video conversion:    disabled")
    else:
        duration_text = (
            f"{args.duration} s"
            if args.duration > 0
            else "continuous until Ctrl+C"
        )
        print(f"Duration:            {duration_text}")

        if args.source == "file":
            print(
                "Input looping:       "
                + ("Python feeder" if args.loop_input else "disabled")
            )
        elif args.source == "rtsp":
            print("Input mode:          real RTSP camera")

        print(f"Video setting:       {args.video_size}, {args.fps} fps")
        print(f"Video bitrate:       {args.video_bitrate} kbit/s")
    print(f"Payload size:        {args.payload_size} bytes")
    print(f"Gross BPSK rate:     {gross_bps:,.0f} bit/s")
    packet_wire_bits = len(PREAMBLE_BITS) + (HEADER_SIZE + args.payload_size) * 8
    payload_efficiency = args.payload_size * 8 / packet_wire_bits
    ideal_payload_rate = gross_bps * payload_efficiency
    print(f"Ideal payload rate:  {ideal_payload_rate:,.0f} bit/s before USB/Python/retries")
    print(f"Samples per bit:     {args.samples_per_bit}")
    print(f"RX captures/attempt: {args.captures_per_attempt}")
    print(f"Fresh RX captures:   {args.fresh_rx_captures}")
    print(f"Fresh RX pause:      {args.fresh_rx_pause:.3f} s")
    print(f"Startup extra hold:  {args.startup_extra_hold:.3f} s")
    print(f"RX warmup captures:  {args.rx_warmup_captures}")
    print(f"Sync candidates:     {args.sync_candidates}")
    print(f"Fast sync candidates:{args.fast_sync_candidates}")
    print(
        "Phase tracking:      "
        + ("disabled" if args.disable_phase_tracking else "enabled")
    )
    print(
        f"Playback prebuffer:  "
        f"{args.playback_prebuffer_bytes:,} bytes"
    )
    print(f"TX hold frames:      {args.tx_hold_frames:.2f}")
    print(f"Soft reset after:    {args.soft_reset_after} failed attempt(s)")
    print(f"Strong resync after: {args.hard_reset_after} failed attempt(s)")
    print(f"Periodic resync:     every {args.periodic_reset_packets} packet(s)")
    print("Packet padding:      balanced 0xAA/0x55")
    if args.source == "rtsp":
        print("FFmpeg CFR method:   fps video filter (legacy compatible)")
        print(
            f"FFmpeg pipe read:    raw os.read(), "
            f"{args.camera_read_size:,} bytes maximum"
        )
        print(
            f"RTSP startup timeout:{args.rtsp_timeout_seconds:.1f} s"
        )
    if not args.copy_original:
        print(f"Encoder preset:      {args.encoder_preset}")
        print(f"Keyframe interval:   {args.keyint_seconds:g} s")
    print(
        "Note: useful Python loopback throughput is much lower "
        "than the gross BPSK rate."
    )
    print(f"TX stream saved as:  {args.tx_save}")
    print(f"RX stream saved as:  {args.rx_save}")

    sdr = None
    rx_buffer_size = None

    if not args.software_only:
        frame_samples = radio_frame_samples(
            args.payload_size,
            args.samples_per_bit,
        )

        required_rx_samples = max(
            32_768,
            int(frame_samples * args.rx_buffer_frames),
        )
        rx_buffer_size = next_power_of_two(required_rx_samples)

        print(f"RX buffer:           {rx_buffer_size:,} samples")

        # One Pluto object controls both TX and RX.
        sdr = open_pluto(args, rx_buffer_size)

    sequence = 0
    recovered_bytes = 0
    lost_packets = 0

    total_attempts = 0
    failed_attempts = 0
    total_rx_captures = 0
    rx_only_refreshes = 0
    soft_resyncs = 0
    strong_resyncs = 0
    tx_upload_count = 0
    tx_destroy_count = 0

    preferred_phase = None
    fast_phase_hits = 0
    full_phase_fallbacks = 0
    decode_calls = 0

    timing = {
        "tx_upload": 0.0,
        "tx_hold": 0.0,
        "rx_transfer": 0.0,
        "rx_decode": 0.0,
        "rx_refresh": 0.0,
        "rx_soft_resync": 0.0,
        "radio_strong_resync": 0.0,
        "tx_destroy": 0.0,
        "file_and_player_write": 0.0,
    }

    start_time = time.perf_counter()

    try:
        with (
            args.tx_save.open("wb") as tx_file,
            args.rx_save.open("wb") as rx_file,
        ):
            while True:
                payload = read_stream_chunk(camera.stdout, args.payload_size)

                if not payload:
                    camera_return_code = camera.poll()

                    if sequence == 0:
                        print(
                            "ERROR: FFmpeg/camera produced no HEVC payload."
                        )
                        print(
                            f"FFmpeg return code: {camera_return_code}"
                        )

                        if isinstance(
                            camera,
                            BufferedLivePipeCamera,
                        ):
                            if camera.reader_error is not None:
                                print(
                                    "RTSP reader error: "
                                    f"{camera.reader_error}"
                                )

                            if camera.recent_stderr:
                                print("Recent FFmpeg messages:")

                                for message in camera.recent_stderr[-8:]:
                                    print(f"  {message}")

                    break

                tx_file.write(payload)

                if args.software_only:
                    received_payload = payload
                    metric = 1.0
                    recovered_phase = 0

                else:
                    if (
                        args.periodic_reset_packets > 0
                        and sequence > 0
                        and sequence % args.periodic_reset_packets == 0
                    ):
                        soft_resyncs += 1
                        stage_start = time.perf_counter()
                        inplace_rx_resync(
                            sdr,
                            args,
                            rx_buffer_size,
                            reason=(
                                f"periodic maintenance before packet "
                                f"{sequence}"
                            ),
                        )
                        timing["rx_soft_resync"] += (
                            time.perf_counter() - stage_start
                        )

                    flags = FLAG_START if sequence == 0 else 0

                    timestamp_ms = int(
                        (time.perf_counter() - start_time) * 1000
                    )

                    packet = build_packet(
                        sequence=sequence,
                        timestamp_ms=timestamp_ms,
                        flags=flags,
                        payload=payload,
                        payload_size=args.payload_size,
                    )

                    iq = packet_to_iq(
                        packet,
                        args.samples_per_bit,
                    )

                    success = False
                    tx_active = False
                    first_tx_upload = True
                    last_error = None

                    try:
                        for attempt in range(1, args.retries + 1):
                            total_attempts += 1
                            just_uploaded = False

                            # Upload the packet only when TX is not already
                            # active. Normal retries keep the same cyclic TX
                            # waveform running.
                            if not tx_active:
                                if (
                                    args.flush_rx_every_packet
                                    and hasattr(sdr, "rx_destroy_buffer")
                                ):
                                    try:
                                        sdr.rx_destroy_buffer()
                                    except Exception:
                                        pass

                                sdr.tx_cyclic_buffer = True

                                stage_start = time.perf_counter()
                                sdr.tx(iq)
                                timing["tx_upload"] += (
                                    time.perf_counter() - stage_start
                                )
                                tx_upload_count += 1
                                tx_active = True
                                just_uploaded = True

                                frame_time = len(iq) / args.sample_rate
                                hold_time = max(
                                    args.minimum_tx_hold,
                                    frame_time * args.tx_hold_frames,
                                )

                                if sequence == 0 and first_tx_upload:
                                    hold_time += args.startup_extra_hold

                                first_tx_upload = False

                                stage_start = time.perf_counter()
                                time.sleep(hold_time)
                                timing["tx_hold"] += (
                                    time.perf_counter() - stage_start
                                )

                            last_error = None

                            # The warm-up capture is only necessary after a new
                            # TX upload, including a retransmission following a
                            # strong radio reset. RX-only retries do not discard
                            # another complete capture.
                            warmup_count = (
                                args.rx_warmup_captures
                                if just_uploaded
                                else 0
                            )

                            for _ in range(warmup_count):
                                stage_start = time.perf_counter()
                                sdr.rx()
                                timing["rx_transfer"] += (
                                    time.perf_counter() - stage_start
                                )
                                total_rx_captures += 1

                            for capture_number in range(
                                1,
                                args.captures_per_attempt + 1,
                            ):
                                stage_start = time.perf_counter()
                                rx_samples = sdr.rx()
                                timing["rx_transfer"] += (
                                    time.perf_counter() - stage_start
                                )
                                total_rx_captures += 1

                                try:
                                    decode_calls += 1
                                    phase_before = (
                                        None
                                        if args.disable_phase_tracking
                                        else preferred_phase
                                    )

                                    stage_start = time.perf_counter()
                                    try:
                                        (
                                            fields,
                                            metric,
                                            recovered_phase,
                                        ) = recover_packet(
                                            rx_samples,
                                            payload_size=args.payload_size,
                                            samples_per_bit=args.samples_per_bit,
                                            expected_sequence=sequence,
                                            candidates_per_phase=args.sync_candidates,
                                            preferred_phase=phase_before,
                                            fast_candidates=args.fast_sync_candidates,
                                        )
                                    finally:
                                        timing["rx_decode"] += (
                                            time.perf_counter() - stage_start
                                        )

                                    if (
                                        phase_before is not None
                                        and recovered_phase == phase_before
                                    ):
                                        fast_phase_hits += 1
                                    elif phase_before is not None:
                                        full_phase_fallbacks += 1

                                    candidate_payload = fields["payload"]

                                    if candidate_payload != payload:
                                        raise ValueError(
                                            "Recovered payload does not match "
                                            "the current transmitted payload."
                                        )

                                    received_payload = candidate_payload
                                    success = True
                                    break

                                except Exception as error:
                                    last_error = error

                            if success:
                                break

                            # First recovery level: rebuild only the RX buffer
                            # while the cyclic TX packet continues.
                            if args.fresh_rx_captures > 0:
                                rx_only_refreshes += 1
                                stage_start = time.perf_counter()
                                refresh_rx_buffer(
                                    sdr,
                                    pause=args.fresh_rx_pause,
                                )
                                timing["rx_refresh"] += (
                                    time.perf_counter() - stage_start
                                )

                                fresh_error = last_error

                                for fresh_capture in range(
                                    1,
                                    args.fresh_rx_captures + 1,
                                ):
                                    stage_start = time.perf_counter()
                                    rx_samples = sdr.rx()
                                    timing["rx_transfer"] += (
                                        time.perf_counter() - stage_start
                                    )
                                    total_rx_captures += 1

                                    try:
                                        decode_calls += 1
                                        phase_before = (
                                            None
                                            if args.disable_phase_tracking
                                            else preferred_phase
                                        )

                                        stage_start = time.perf_counter()
                                        try:
                                            (
                                                fields,
                                                metric,
                                                recovered_phase,
                                            ) = recover_packet(
                                                rx_samples,
                                                payload_size=args.payload_size,
                                                samples_per_bit=args.samples_per_bit,
                                                expected_sequence=sequence,
                                                candidates_per_phase=args.sync_candidates,
                                                preferred_phase=phase_before,
                                                fast_candidates=args.fast_sync_candidates,
                                            )
                                        finally:
                                            timing["rx_decode"] += (
                                                time.perf_counter() - stage_start
                                            )

                                        if (
                                            phase_before is not None
                                            and recovered_phase == phase_before
                                        ):
                                            fast_phase_hits += 1
                                        elif phase_before is not None:
                                            full_phase_fallbacks += 1

                                        candidate_payload = fields["payload"]

                                        if candidate_payload != payload:
                                            raise ValueError(
                                                "Recovered payload does not "
                                                "match the current transmitted "
                                                "payload."
                                            )

                                        received_payload = candidate_payload
                                        success = True

                                        print(
                                            f"RX-only refresh recovered "
                                            f"packet {sequence} on fresh "
                                            f"capture {fresh_capture}."
                                        )
                                        break

                                    except Exception as error:
                                        fresh_error = error

                                if success:
                                    break

                                last_error = fresh_error

                            failed_attempts += 1

                            print(
                                f"Packet {sequence}, attempt "
                                f"{attempt}/{args.retries}: {last_error}"
                            )

                            # A strong reset still rebuilds the whole radio and
                            # therefore invalidates the cyclic TX buffer.
                            if (
                                args.hard_reset_after > 0
                                and attempt % args.hard_reset_after == 0
                                and attempt < args.retries
                            ):
                                strong_resyncs += 1
                                stage_start = time.perf_counter()
                                sdr = inplace_radio_resync(
                                    sdr,
                                    args,
                                    rx_buffer_size,
                                    reason=(
                                        f"packet {sequence} failed "
                                        f"{attempt} consecutive attempts"
                                    ),
                                    stronger=True,
                                )
                                timing["radio_strong_resync"] += (
                                    time.perf_counter() - stage_start
                                )
                                tx_active = False
                                continue

                            # A soft reset now touches only RX and leaves the
                            # current TX waveform active.
                            if (
                                args.soft_reset_after > 0
                                and attempt % args.soft_reset_after == 0
                                and attempt < args.retries
                            ):
                                soft_resyncs += 1
                                stage_start = time.perf_counter()
                                inplace_rx_resync(
                                    sdr,
                                    args,
                                    rx_buffer_size,
                                    reason=(
                                        f"packet {sequence} failed "
                                        f"{attempt} consecutive attempts"
                                    ),
                                )
                                timing["rx_soft_resync"] += (
                                    time.perf_counter() - stage_start
                                )

                    finally:
                        # Destroy once when the packet is complete or abandoned,
                        # instead of once per failed attempt.
                        if tx_active:
                            stage_start = time.perf_counter()
                            try:
                                sdr.tx_destroy_buffer()
                            except Exception:
                                pass
                            timing["tx_destroy"] += (
                                time.perf_counter() - stage_start
                            )
                            tx_destroy_count += 1

                    if not success:
                        lost_packets += 1

                        print(
                            f"WARNING: Packet {sequence} was lost after "
                            f"{args.retries} attempts. "
                            "Continuing to the next live packet."
                        )

                        if args.strict:
                            raise RuntimeError(
                                f"Packet {sequence} failed."
                            )

                        # Real live video normally continues after packet loss.
                        # x265 repeats VPS/SPS/PPS and sends an IDR every second,
                        # allowing FFplay to recover at the next keyframe.
                        sequence += 1
                        continue

                if (
                    not args.software_only
                    and not args.disable_phase_tracking
                ):
                    preferred_phase = recovered_phase

                stage_start = time.perf_counter()
                rx_file.write(received_payload)
                recovered_bytes += len(received_payload)

                if (
                    not args.no_display
                    and player is None
                    and args.playback_prebuffer_bytes > 0
                ):
                    playback_pending.extend(received_payload)

                    if (
                        len(playback_pending)
                        >= args.playback_prebuffer_bytes
                    ):
                        player = start_player()

                        if player.stdin is not None:
                            player.stdin.write(playback_pending)
                            player.stdin.flush()

                        playback_pending.clear()

                elif (
                    player is not None
                    and player.stdin is not None
                ):
                    try:
                        player.stdin.write(received_payload)
                        player.stdin.flush()
                    except BrokenPipeError:
                        print("FFplay was closed.")
                        player = None

                timing["file_and_player_write"] += (
                    time.perf_counter() - stage_start
                )

                elapsed = time.perf_counter() - start_time
                useful_rate = (
                    recovered_bytes * 8 / elapsed
                    if elapsed > 0
                    else 0
                )

                backlog_text = ""

                if isinstance(camera, BufferedLivePipeCamera):
                    backlog_seconds = (
                        camera.buffered_bytes * 8.0
                        / max(args.video_bitrate * 1000.0, 1.0)
                    )
                    backlog_text = (
                        f", source_buffer={camera.buffered_bytes:,} B"
                        f"(~{backlog_seconds:.1f}s)"
                    )

                print(
                    f"RX packet={sequence}, "
                    f"bytes={len(received_payload)}, "
                    f"sync={metric:.3f}, "
                    f"phase={recovered_phase}, "
                    f"rate={useful_rate:,.0f} bit/s"
                    f"{backlog_text}"
                )

                sequence += 1

                if (
                    args.timing_report_every > 0
                    and sequence % args.timing_report_every == 0
                ):
                    measured = sum(timing.values())
                    print(
                        "TIMING: "
                        f"packets={sequence}, "
                        f"tx_upload={timing['tx_upload']:.3f}s, "
                        f"rx_transfer={timing['rx_transfer']:.3f}s, "
                        f"rx_decode={timing['rx_decode']:.3f}s, "
                        f"measured={measured:.3f}s"
                    )

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        release_pluto(sdr)

        if camera.poll() is None:
            camera.terminate()

        # If a short source ended before reaching the requested prebuffer,
        # still send the collected bytes to FFplay rather than showing nothing.
        if (
            player is None
            and playback_pending
            and not args.no_display
        ):
            print(
                "Source ended before the playback prebuffer was reached; "
                "playing the available received bytes."
            )
            player = start_player()

            if player.stdin is not None:
                try:
                    player.stdin.write(playback_pending)
                    player.stdin.flush()
                except BrokenPipeError:
                    player = None

            playback_pending.clear()

        if player is not None:
            if player.stdin is not None:
                try:
                    player.stdin.close()
                except Exception:
                    pass

            try:
                player.wait(timeout=3)
            except subprocess.TimeoutExpired:
                player.terminate()

    print("\n========== RESULT ==========")
    print(f"Packets received:   {sequence}")
    print(f"Bytes received:     {recovered_bytes:,}")
    print(f"Lost SDR packets:   {lost_packets}")
    print(f"Total TX attempts:  {total_attempts}")
    print(f"Failed attempts:    {failed_attempts}")
    print(f"RX captures:        {total_rx_captures}")
    print(f"RX-only refreshes:  {rx_only_refreshes}")
    print(f"Soft resyncs:       {soft_resyncs}")
    print(f"Strong resyncs:     {strong_resyncs}")
    print(f"TX uploads:         {tx_upload_count}")
    print(f"TX destroys:        {tx_destroy_count}")
    print(f"Decode calls:       {decode_calls}")
    print(f"Fast phase hits:    {fast_phase_hits}")
    print(f"Full phase fallback:{full_phase_fallbacks}")

    total_elapsed = time.perf_counter() - start_time
    measured_time = sum(timing.values())
    unmeasured_time = max(0.0, total_elapsed - measured_time)

    print("\n========== TIMING DIAGNOSTIC ==========")
    print(f"Total elapsed:          {total_elapsed:.3f} s")

    for name, seconds in timing.items():
        percent = (
            seconds * 100.0 / total_elapsed
            if total_elapsed > 0
            else 0.0
        )
        print(
            f"{name:22s} {seconds:9.3f} s "
            f"({percent:5.1f}%)"
        )

    print(
        f"{'unmeasured_python':22s} {unmeasured_time:9.3f} s "
        f"({(unmeasured_time * 100.0 / total_elapsed if total_elapsed > 0 else 0):5.1f}%)"
    )

    if total_rx_captures > 0:
        print(
            "Average RX transfer:   "
            f"{timing['rx_transfer'] * 1000 / total_rx_captures:.3f} ms/capture"
        )

    if decode_calls > 0:
        print(
            "Average RX decode:     "
            f"{timing['rx_decode'] * 1000 / decode_calls:.3f} ms/call"
        )

    if isinstance(camera, BufferedLivePipeCamera):
        print("\n========== LIVE SOURCE BUFFER ==========")
        print(
            f"Total FFmpeg bytes read: {camera.total_bytes_read:,}"
        )
        print(
            f"Peak queued bytes:       {camera.high_water_bytes:,}"
        )
        print(
            f"Bytes still queued:      {camera.buffered_bytes:,}"
        )

        estimated_peak_delay = (
            camera.high_water_bytes * 8.0
            / max(args.video_bitrate * 1000.0, 1.0)
        )
        print(
            f"Estimated peak delay:    {estimated_peak_delay:.2f} s "
            f"at target bitrate"
        )

        if camera.reader_error is not None:
            print(
                f"Camera reader warning:   {camera.reader_error}"
            )

    if (
        not args.copy_original
        and args.tx_save.exists()
    ):
        encoded_bytes = args.tx_save.stat().st_size
        encoded_frames = probe_hevc_frame_count(
            args.tx_save,
            args.fps,
        )

        actual_video_duration = (
            encoded_frames / args.fps
            if encoded_frames is not None and args.fps > 0
            else None
        )

        encoded_rate = (
            encoded_bytes * 8.0 / actual_video_duration
            if actual_video_duration is not None
            and actual_video_duration > 0
            else None
        )

        achieved_link_rate = (
            recovered_bytes * 8.0 / total_elapsed
            if total_elapsed > 0
            else 0.0
        )

        print("\n========== STREAM RATE CHECK ==========")
        print(f"Encoded TX bytes:       {encoded_bytes:,}")

        if encoded_frames is not None:
            print(f"Encoded video frames:   {encoded_frames:,}")

        if actual_video_duration is not None:
            print(
                f"Produced video duration: {actual_video_duration:.3f} s "
                f"(requested {args.duration} s)"
            )

        if encoded_rate is not None:
            print(
                f"Actual encoded bitrate: {encoded_rate / 1000:.3f} kbit/s"
            )
        else:
            print("Actual encoded bitrate: unavailable")

        print(
            f"Achieved SDR bitrate:   {achieved_link_rate / 1000:.3f} kbit/s"
        )

        if (
            actual_video_duration is not None
            and args.duration > 0
            and actual_video_duration + (1.0 / max(args.fps, 1))
                < args.duration
        ):
            print(
                "WARNING: FFmpeg produced less video than requested. "
                "Check the input/looping source."
            )

        if encoded_rate is not None:
            if encoded_rate >= achieved_link_rate:
                print(
                    "WARNING: The encoded source rate is not below the SDR "
                    "delivery rate. Playback will eventually stall even with "
                    "a prebuffer."
                )
            else:
                margin = achieved_link_rate - encoded_rate
                print(
                    f"Streaming margin:       {margin / 1000:.3f} kbit/s"
                )

    if args.tx_save.exists() and args.rx_save.exists():
        tx_hash = sha256_file(args.tx_save)
        rx_hash = sha256_file(args.rx_save)

        print(f"TX SHA-256:         {tx_hash}")
        print(f"RX SHA-256:         {rx_hash}")

        if sequence == 0 or recovered_bytes == 0:
            print(
                "RESULT: No SDR video payload was transmitted. Matching "
                "empty-file SHA-256 hashes are not a successful stream."
            )
        elif tx_hash == rx_hash:
            print("RESULT: TX and RX H.265 streams are identical.")
        else:
            print("RESULT: TX and RX streams are different.")


if __name__ == "__main__":
    main()
