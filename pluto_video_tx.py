#!/usr/bin/env python3
"""
pluto_video_tx.py

Two-Pluto video transmitter.

Flow:
    RTSP camera/file -> FFmpeg low-bitrate HEVC -> packet framing
    -> BPSK waveform -> PlutoSDR transmitter

The receiver is pluto_video_rx.py.

Important:
- This is a one-way streaming protocol. There is no ACK/retransmission link.
- Each packet is transmitted cyclically for a short dwell time. The receiver
  de-duplicates repeated copies.
- TX and RX scripts must use the same frequency, sample rate, samples/bit,
  payload size and protocol constants.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from collections import deque
import os
from pathlib import Path
import re
import secrets
import shlex
import struct
import subprocess
import sys
import threading
import time
import zlib

import numpy as np

try:
    import adi
except ImportError as exc:
    raise SystemExit(
        "pyadi-iio is required. Install it with: pip install pyadi-iio"
    ) from exc


# ---------------------------------------------------------------------------
# Local camera configuration
# ---------------------------------------------------------------------------
# Replace PASSWORD once. The URL is not printed in normal terminal output.
CAMERA_URL = (
    "rtsp://admin:PASSWORD@192.168.1.2:554/Preview_01_sub"
)



# ---------------------------------------------------------------------------
# Persistent experiment logging
# ---------------------------------------------------------------------------

class _TeeStream:
    """Write the same text to the terminal and the experiment log."""

    def __init__(self, console, log_file, lock: threading.Lock):
        self.console = console
        self.log_file = log_file
        self.lock = lock
        self.encoding = getattr(console, "encoding", "utf-8")

    def write(self, text: str) -> int:
        if not text:
            return 0

        with self.lock:
            self.console.write(text)
            self.log_file.write(text)
            self.console.flush()
            self.log_file.flush()

        return len(text)

    def flush(self) -> None:
        with self.lock:
            self.console.flush()
            self.log_file.flush()

    def isatty(self) -> bool:
        return bool(getattr(self.console, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self.console.fileno()


class RunLog:
    """
    Mirror stdout and stderr into a timestamped UTF-8 text log.

    The log contains the command line, settings, FFmpeg messages, packet
    status, errors and final statistics while preserving terminal output.
    """

    def __init__(
        self,
        role: str,
        log_dir: Path,
        explicit_path: Path | None,
    ):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = (
            explicit_path
            if explicit_path is not None
            else log_dir / f"{role}_{timestamp}.log"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._file = self.path.open(
            "w",
            encoding="utf-8",
            buffering=1,
        )
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self._lock = threading.Lock()

        sys.stdout = _TeeStream(
            self._original_stdout,
            self._file,
            self._lock,
        )
        sys.stderr = _TeeStream(
            self._original_stderr,
            self._file,
            self._lock,
        )

    def close(self) -> None:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            sys.stdout = self._original_stdout
            sys.stderr = self._original_stderr
            self._file.close()


def print_log_header(run_log: RunLog, role: str) -> None:
    print("========== EXPERIMENT LOG ==========")
    print(f"Role:                {role}")
    print(
        "Started:             "
        f"{datetime.now().astimezone().isoformat(timespec='seconds')}"
    )
    print(
        "Command:             "
        + " ".join(shlex.quote(argument) for argument in sys.argv)
    )
    print(f"Log file:            {run_log.path}")
    print("Pluto connection:    direct USB context usb:")
    print()

# ---------------------------------------------------------------------------
# Shared over-the-air protocol
# ---------------------------------------------------------------------------

MAGIC = b"P2V1"

# magic, session, sequence, timestamp_ms, payload_length, flags, crc32
HEADER_FORMAT = "!4sIIIHBI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

FLAG_START = 0x01
FLAG_END = 0x02

PREAMBLE_BLOCK_BITS = 64
PREAMBLE_REPEATS = 8

_rng = np.random.default_rng(20260802)
_base_preamble_bits = _rng.integers(
    0,
    2,
    PREAMBLE_BLOCK_BITS,
    dtype=np.uint8,
)
PREAMBLE_BITS = np.tile(
    _base_preamble_bits,
    PREAMBLE_REPEATS,
)
PREAMBLE_SYMBOLS = (
    1.0 - 2.0 * PREAMBLE_BITS.astype(np.float32)
).astype(np.complex64)


def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def balanced_padding(length: int, sequence: int) -> bytes:
    if length <= 0:
        return b""

    first = 0xAA if sequence % 2 == 0 else 0x55
    second = 0x55 if first == 0xAA else 0xAA
    pattern = bytes((first, second))
    return (pattern * ((length + 1) // 2))[:length]


def build_packet(
    session: int,
    sequence: int,
    timestamp_ms: int,
    flags: int,
    payload: bytes,
    payload_size: int,
) -> bytes:
    if len(payload) > payload_size:
        raise ValueError("Payload exceeds configured payload size.")

    crc = zlib.crc32(payload) & 0xFFFFFFFF

    header = struct.pack(
        HEADER_FORMAT,
        MAGIC,
        session & 0xFFFFFFFF,
        sequence & 0xFFFFFFFF,
        timestamp_ms & 0xFFFFFFFF,
        len(payload),
        flags & 0xFF,
        crc,
    )

    return (
        header
        + payload
        + balanced_padding(
            payload_size - len(payload),
            sequence,
        )
    )


def packet_to_iq(
    packet: bytes,
    samples_per_bit: int,
    scale: float,
) -> np.ndarray:
    packet_bits = bytes_to_bits(packet)
    packet_symbols = (
        1.0 - 2.0 * packet_bits.astype(np.float32)
    ).astype(np.complex64)

    symbols = np.concatenate(
        (PREAMBLE_SYMBOLS, packet_symbols)
    )
    samples = np.repeat(symbols, samples_per_bit)

    guard = np.zeros(
        16 * samples_per_bit,
        dtype=np.complex64,
    )

    return (
        np.concatenate((guard, samples, guard))
        * scale
    ).astype(np.complex64)


def frame_sample_count(
    payload_size: int,
    samples_per_bit: int,
) -> int:
    packet_bits = (HEADER_SIZE + payload_size) * 8

    return (
        (len(PREAMBLE_BITS) + packet_bits)
        * samples_per_bit
        + 32 * samples_per_bit
    )


# ---------------------------------------------------------------------------
# Video sources
# ---------------------------------------------------------------------------

class DirectFileSource:
    def __init__(self, path: Path, loop: bool):
        self.path = path
        self.loop = loop
        self.handle = path.open("rb")
        self.total_bytes_read = 0
        self.high_water_bytes = 0
        self.buffered_bytes = 0
        self.reader_error = None

    def read(self, size: int) -> bytes:
        output = bytearray()

        while len(output) < size:
            chunk = self.handle.read(size - len(output))

            if chunk:
                output.extend(chunk)
                self.total_bytes_read += len(chunk)
                continue

            if not self.loop:
                break

            self.handle.seek(0)

        return bytes(output)

    def poll(self):
        return None

    def terminate(self):
        self.handle.close()


class BufferedFfmpegSource:
    """Background FFmpeg reader with bounded delay buffering."""

    def __init__(
        self,
        command: list[str],
        max_buffer_bytes: int,
        read_size: int,
        startup_timeout: float,
        idle_timeout: float,
    ):
        self.max_buffer_bytes = max(4096, max_buffer_bytes)
        self.read_size = min(4096, max(256, read_size))
        self.startup_timeout = max(1.0, startup_timeout)
        self.idle_timeout = max(1.0, idle_timeout)

        self._buffer = bytearray()
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._reader_finished = False
        self._reader_error: Exception | None = None
        self._stderr_lines: deque[str] = deque(maxlen=30)

        self._started_at = time.monotonic()
        self._last_output_at = self._started_at
        self._total_bytes_read = 0
        self._high_water_bytes = 0

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        if self.process.stdout is None or self.process.stderr is None:
            self.process.terminate()
            raise RuntimeError("Could not open FFmpeg pipes.")

        self._stdout_thread = threading.Thread(
            target=self._stdout_loop,
            daemon=True,
            name="ffmpeg-hevc-reader",
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            daemon=True,
            name="ffmpeg-stderr-reader",
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    @staticmethod
    def _redact(text: str) -> str:
        return re.sub(
            r"(rtsp://[^:/@\s]+:)[^@/\s]+(@)",
            r"\1***\2",
            text,
        )

    def _stderr_loop(self) -> None:
        assert self.process.stderr is not None

        while not self._stop.is_set():
            raw = self.process.stderr.readline()

            if not raw:
                break

            line = self._redact(
                raw.decode("utf-8", errors="replace").rstrip()
            )

            if line:
                self._stderr_lines.append(line)
                print(f"[FFmpeg] {line}", flush=True)

    def _stdout_loop(self) -> None:
        assert self.process.stdout is not None
        fd = self.process.stdout.fileno()

        try:
            while not self._stop.is_set():
                chunk = os.read(fd, self.read_size)

                if not chunk:
                    break

                with self._condition:
                    while (
                        len(self._buffer) + len(chunk)
                        > self.max_buffer_bytes
                        and not self._stop.is_set()
                    ):
                        self._condition.wait(timeout=0.1)

                    if self._stop.is_set():
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
            if not self._stop.is_set():
                self._reader_error = error

        finally:
            with self._condition:
                self._reader_finished = True
                self._condition.notify_all()

    def read(self, size: int) -> bytes:
        if size <= 0:
            return b""

        with self._condition:
            while (
                len(self._buffer) < size
                and not self._reader_finished
                and not self._stop.is_set()
            ):
                now = time.monotonic()
                startup = self._total_bytes_read == 0
                elapsed = (
                    now - self._started_at
                    if startup
                    else now - self._last_output_at
                )
                timeout = (
                    self.startup_timeout
                    if startup
                    else self.idle_timeout
                )

                if elapsed >= timeout:
                    mode = "startup" if startup else "idle"
                    recent = " | ".join(self.recent_stderr[-5:])
                    message = (
                        f"FFmpeg {mode} timeout after {timeout:.1f}s."
                    )

                    if recent:
                        message += f" Recent output: {recent}"

                    self._reader_error = TimeoutError(message)
                    self._reader_finished = True
                    break

                self._condition.wait(timeout=0.1)

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
        self._stop.set()

        with self._condition:
            self._condition.notify_all()

        if self.process.poll() is None:
            self.process.terminate()

        self._stdout_thread.join(timeout=2)
        self._stderr_thread.join(timeout=2)

        if self.process.poll() is None:
            self.process.kill()

    @property
    def buffered_bytes(self) -> int:
        with self._condition:
            return len(self._buffer)

    @property
    def total_bytes_read(self) -> int:
        return self._total_bytes_read

    @property
    def high_water_bytes(self) -> int:
        return self._high_water_bytes

    @property
    def reader_error(self) -> Exception | None:
        return self._reader_error

    @property
    def recent_stderr(self) -> list[str]:
        return list(self._stderr_lines)


def x265_parameters(args: argparse.Namespace) -> str:
    keyint = max(1, int(round(args.fps * args.keyint_seconds)))
    vbv = max(args.video_bitrate * 4, 4)

    return (
        "repeat-headers=1:"
        "aud=0:"
        f"keyint={keyint}:"
        f"min-keyint={keyint}:"
        "scenecut=0:"
        "bframes=0:"
        f"vbv-maxrate={args.video_bitrate}:"
        f"vbv-bufsize={vbv}"
    )


def parse_video_size(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width = int(width_text)
        height = int(height_text)
    except (ValueError, AttributeError) as error:
        raise ValueError(
            "Video size must use WIDTHxHEIGHT, for example 320x180."
        ) from error

    if width < 16 or height < 16:
        raise ValueError("Video dimensions must be at least 16 pixels.")

    return width, height


def start_rtsp_source(args: argparse.Namespace) -> BufferedFfmpegSource:
    camera_url = args.camera_url or CAMERA_URL

    if not camera_url or "PASSWORD" in camera_url:
        raise ValueError(
            "Edit CAMERA_URL near the top of pluto_video_tx.py and replace "
            "PASSWORD with the camera password."
        )

    width, height = parse_video_size(args.video_size)

    video_filter = (
        f"scale={width}:{height}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={args.fps},format=yuv420p"
    )

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
            x265_parameters(args),
            "-flush_packets",
            "1",
            "-f",
            "hevc",
            "pipe:1",
        ]
    )

    return BufferedFfmpegSource(
        command=command,
        max_buffer_bytes=args.camera_buffer_bytes,
        read_size=args.camera_read_size,
        startup_timeout=args.source_timeout,
        idle_timeout=max(args.source_timeout * 2, 15),
    )


def open_tx_pluto(args: argparse.Namespace):
    device = adi.Pluto(uri=args.uri)

    device.sample_rate = int(args.sample_rate)
    device.tx_lo = int(args.frequency)
    device.tx_rf_bandwidth = int(args.sample_rate)
    device.tx_hardwaregain_chan0 = float(args.tx_gain)
    device.tx_enabled_channels = [0]
    device.tx_cyclic_buffer = True

    return device


def destroy_tx_buffer(device) -> None:
    try:
        device.tx_destroy_buffer()
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transmit low-bitrate HEVC video with PlutoSDR #1."
    )

    parser.add_argument(
        "--source",
        choices=("rtsp", "file"),
        default="rtsp",
    )
    parser.add_argument(
        "--camera-url",
        help="Optional RTSP URL override.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Raw H.265 file for --source file.",
    )
    parser.add_argument(
        "--loop-file",
        action="store_true",
        help="Loop the raw H.265 input file.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="RTSP duration in seconds; 0 means until Ctrl+C.",
    )
    parser.add_argument("--video-size", default="320x180")
    parser.add_argument("--fps", type=int, default=5)
    parser.add_argument("--video-bitrate", type=int, default=5)
    parser.add_argument("--keyint-seconds", type=float, default=10)
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
    )
    parser.add_argument(
        "--rtsp-transport",
        choices=("tcp", "udp"),
        default="tcp",
    )
    parser.add_argument(
        "--camera-buffer-bytes",
        type=int,
        default=512 * 1024,
    )
    parser.add_argument(
        "--camera-read-size",
        type=int,
        default=1024,
    )
    parser.add_argument(
        "--source-timeout",
        type=float,
        default=15,
    )

    parser.add_argument(
        "--uri",
        default="usb:",
        help=(
            "TX Pluto libiio context. Default: usb:. "
            "Use one directly connected Pluto on this computer."
        ),
    )
    parser.add_argument(
        "--frequency",
        type=int,
        default=915_000_000,
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=2_000_000,
    )
    parser.add_argument(
        "--samples-per-bit",
        type=int,
        default=4,
    )
    parser.add_argument(
        "--payload-size",
        type=int,
        default=400,
    )
    parser.add_argument(
        "--packet-airtime",
        type=float,
        default=0.10,
        help=(
            "Seconds each packet remains in the cyclic TX buffer. "
            "Longer is more reliable; shorter is faster."
        ),
    )
    parser.add_argument(
        "--inter-packet-gap",
        type=float,
        default=0.002,
    )
    parser.add_argument(
        "--tx-gain",
        type=float,
        default=-50,
    )
    parser.add_argument(
        "--iq-scale",
        type=float,
        default=float(2**13),
    )
    parser.add_argument(
        "--tx-save",
        type=Path,
        default=Path("two_pluto_transmitted.h265"),
    )
    parser.add_argument(
        "--status-every",
        type=int,
        default=1,
        help=(
            "Write a status line every N new packets. Default 1 gives a "
            "detailed experiment log."
        ),
    )

    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("logs"),
        help=(
            "Directory for automatic timestamped experiment logs. "
            "Default: logs."
        ),
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help=(
            "Optional exact log path. Overrides --log-dir and automatic "
            "timestamp naming."
        ),
    )

    return parser


def validate_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.source == "file" and args.input is None:
        parser.error("--input is required with --source file")
    if args.payload_size < 64:
        parser.error("--payload-size must be at least 64")
    if args.samples_per_bit < 2:
        parser.error("--samples-per-bit must be at least 2")
    if args.packet_airtime <= 0:
        parser.error("--packet-airtime must be greater than zero")
    if args.inter_packet_gap < 0:
        parser.error("--inter-packet-gap cannot be negative")
    if args.video_bitrate < 1:
        parser.error("--video-bitrate must be at least 1")
    if args.fps < 1:
        parser.error("--fps must be at least 1")
    if args.camera_buffer_bytes < args.payload_size * 4:
        parser.error(
            "--camera-buffer-bytes must hold at least four payloads"
        )
    parse_video_size(args.video_size)


def run(args: argparse.Namespace) -> int:
    if args.source == "rtsp":
        source = start_rtsp_source(args)
    else:
        assert args.input is not None
        source = DirectFileSource(args.input, args.loop_file)

    session = secrets.randbits(32)
    sequence = 0
    total_payload_bytes = 0
    tx_uploads = 0
    start_time = time.monotonic()
    source_start = start_time
    first_packet = True
    device = None

    frame_samples = frame_sample_count(
        args.payload_size,
        args.samples_per_bit,
    )
    symbol_rate = args.sample_rate / args.samples_per_bit
    gross_rate = symbol_rate
    dwell_payload_rate = (
        args.payload_size * 8 / args.packet_airtime
    )

    print("========== TWO-PLUTO VIDEO TRANSMITTER ==========")
    print(f"Session:             0x{session:08X}")
    print(f"Source:              {args.source}")
    print(f"TX URI:              {args.uri}")
    print(f"Frequency:           {args.frequency:,} Hz")
    print(f"Sample rate:         {args.sample_rate:,} sample/s")
    print(f"Samples per bit:     {args.samples_per_bit}")
    print(f"Gross BPSK rate:     {gross_rate:,.0f} bit/s")
    print(f"Payload size:        {args.payload_size} bytes")
    print(f"Frame samples:       {frame_samples:,}")
    print(f"Packet airtime:      {args.packet_airtime:.3f} s")
    print(
        f"Dwell-limited rate: {dwell_payload_rate:,.0f} bit/s "
        "before upload gaps"
    )
    print(f"TX gain:             {args.tx_gain:.1f} dB")
    print(f"TX file:             {args.tx_save}")
    print("Start pluto_video_rx.py before this transmitter.")
    print("Press Ctrl+C to stop.\n")

    args.tx_save.parent.mkdir(parents=True, exist_ok=True)

    try:
        device = open_tx_pluto(args)

        with args.tx_save.open("wb") as tx_file:
            while True:
                payload = source.read(args.payload_size)

                if not payload:
                    if source.reader_error is not None:
                        print(f"Source error: {source.reader_error}")

                    if args.source == "rtsp" and args.duration == 0:
                        print("Live source ended.")
                    break

                flags = FLAG_START if first_packet else 0
                first_packet = False
                timestamp_ms = int(
                    (time.monotonic() - source_start) * 1000
                )

                packet = build_packet(
                    session=session,
                    sequence=sequence,
                    timestamp_ms=timestamp_ms,
                    flags=flags,
                    payload=payload,
                    payload_size=args.payload_size,
                )
                iq = packet_to_iq(
                    packet,
                    args.samples_per_bit,
                    args.iq_scale,
                )

                destroy_tx_buffer(device)
                device.tx_cyclic_buffer = True
                device.tx(iq)
                tx_uploads += 1

                time.sleep(args.packet_airtime)
                destroy_tx_buffer(device)

                if args.inter_packet_gap > 0:
                    time.sleep(args.inter_packet_gap)

                tx_file.write(payload)
                tx_file.flush()
                total_payload_bytes += len(payload)
                sequence = (sequence + 1) & 0xFFFFFFFF

                if (
                    args.status_every > 0
                    and sequence % args.status_every == 0
                ):
                    elapsed = time.monotonic() - start_time
                    useful_rate = (
                        total_payload_bytes * 8 / elapsed
                        if elapsed > 0
                        else 0
                    )
                    source_buffer = getattr(
                        source,
                        "buffered_bytes",
                        0,
                    )
                    source_delay = (
                        source_buffer * 8
                        / max(args.video_bitrate * 1000, 1)
                    )

                    print(
                        f"TX packet={sequence - 1}, "
                        f"bytes={total_payload_bytes:,}, "
                        f"rate={useful_rate:,.0f} bit/s, "
                        f"source_buffer={source_buffer:,} B "
                        f"(~{source_delay:.1f}s)"
                    )

    except KeyboardInterrupt:
        print("\nStopping transmitter...")

    finally:
        if device is not None:
            destroy_tx_buffer(device)

        source.terminate()

    elapsed = time.monotonic() - start_time
    useful_rate = (
        total_payload_bytes * 8 / elapsed
        if elapsed > 0
        else 0
    )

    print("\n========== TX RESULT ==========")
    print(f"Session:             0x{session:08X}")
    print(f"Packets transmitted: {sequence:,}")
    print(f"Payload bytes:       {total_payload_bytes:,}")
    print(f"TX uploads:          {tx_uploads:,}")
    print(f"Elapsed:             {elapsed:.3f} s")
    print(f"Useful TX rate:      {useful_rate:,.0f} bit/s")
    print(
        f"Peak source buffer:  "
        f"{getattr(source, 'high_water_bytes', 0):,} bytes"
    )
    print(f"Saved stream:        {args.tx_save}")

    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)

    run_log = RunLog(
        role="tx",
        log_dir=args.log_dir,
        explicit_path=args.log_file,
    )

    try:
        print_log_header(run_log, "TRANSMITTER")
        return run(args)
    except Exception as error:
        print()
        print("========== FATAL ERROR ==========")
        print(f"{type(error).__name__}: {error}")
        raise
    finally:
        print()
        print(
            "Finished:            "
            f"{datetime.now().astimezone().isoformat(timespec='seconds')}"
        )
        print(f"Experiment log:      {run_log.path}")
        run_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
