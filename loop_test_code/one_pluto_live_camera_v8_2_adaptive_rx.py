#!/usr/bin/env python3
"""
one_pluto_live_camera_v8_2_adaptive_rx.py

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
import hashlib
import shutil
import struct
import subprocess
import time
import zlib
from pathlib import Path

try:
    import adi
except ModuleNotFoundError:
    adi = None

import numpy as np


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
) -> tuple[dict, float]:
    """
    Recover only the packet currently expected by the receiver.

    The synchronization search uses:
        preamble + magic word + expected sequence number

    This prevents the previous cyclic packet from being accepted.
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

    candidates = []

    for phase in range(samples_per_bit):
        usable = (
            (len(rx_samples) - phase)
            // samples_per_bit
            * samples_per_bit
        )

        if usable <= 0:
            continue

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
            continue

        correlation = np.abs(
            np.correlate(
                symbol_stream,
                sync_symbols,
                mode="valid",
            )
        )

        window_energy = np.convolve(
            np.abs(symbol_stream) ** 2,
            np.ones(len(sync_symbols)),
            mode="valid",
        )

        sync_energy = float(
            np.sum(np.abs(sync_symbols) ** 2)
        )

        metric = correlation / np.sqrt(
            window_energy * sync_energy + 1e-12
        )

        last_valid_start = (
            len(symbol_stream) - complete_frame_bits
        )
        metric = metric[:last_valid_start + 1]

        if len(metric) == 0:
            continue

        count = min(candidates_per_phase, len(metric))
        indexes = np.argpartition(metric, -count)[-count:]

        for index in indexes:
            candidates.append(
                (
                    float(metric[index]),
                    int(index),
                    symbol_stream,
                )
            )

    if not candidates:
        raise ValueError("No complete SDR frame found.")

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    last_error = None

    for metric, start_symbol, symbol_stream in candidates:
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

            return fields, metric

        except Exception as error:
            last_error = error

    raise ValueError(f"No valid packet found: {last_error}")


# ------------------------------------------------------------
# FFmpeg camera sources and FFplay
# ------------------------------------------------------------

def start_generated_camera(args):
    """FFmpeg generates a moving low-bitrate H.265 security-camera view."""

    key_interval = max(1, args.fps)

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
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        f"{args.video_bitrate}k",
        "-maxrate",
        f"{args.video_bitrate}k",
        "-bufsize",
        f"{args.video_bitrate * 2}k",
        "-x265-params",
        (
            f"repeat-headers=1:"
            f"aud=1:"
            f"keyint={key_interval}:"
            f"min-keyint={key_interval}:"
            f"scenecut=0:"
            f"bframes=0"
        ),
        "-f",
        "hevc",
        "pipe:1",
    ]

    print("Starting generated security-camera source.")

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


def next_power_of_two(value: int) -> int:
    """Return a Pluto-friendly power-of-two RX buffer size."""
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def start_file_camera(args):
    """
    Start a raw HEVC file source.

    Default mode loops and re-encodes to a low bitrate.

    With --copy-original, the program reads the original HEVC elementary
    stream once and copies its bytes unchanged. This preserves the original
    resolution, frame rate and encoded data, but it is not real-time when the
    source bitrate exceeds the useful one-Pluto loopback throughput.
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

    else:
        key_interval = max(1, args.fps)

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-re",
            *( ["-stream_loop", "-1"] if args.loop_input else [] ),
            "-f",
            "hevc",
            "-framerate",
            str(args.input_fps),
            "-i",
            str(args.input),
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
            "ultrafast",
            "-tune",
            "zerolatency",
            "-b:v",
            f"{args.video_bitrate}k",
            "-maxrate",
            f"{args.video_bitrate}k",
            "-bufsize",
            f"{args.video_bitrate * 2}k",
            "-x265-params",
            (
                f"repeat-headers=1:"
                f"aud=1:"
                f"keyint={key_interval}:"
                f"min-keyint={key_interval}:"
                f"scenecut=0:"
                f"bframes=0"
            ),
            "-flush_packets",
            "1",
            "-f",
            "hevc",
            "pipe:1",
        ]

        print("Starting file-based security-camera source.")
        print(
            f"Re-encoding as {args.video_size}, "
            f"{args.fps} fps, {args.video_bitrate} kbit/s."
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
            "One-Pluto H.265 transmission with balanced padding, adaptive RX-only recovery and in-place long-run radio recovery."
        )
    )

    parser.add_argument(
        "--source",
        choices=("generated", "file"),
        default="generated",
    )

    parser.add_argument(
        "--input",
        type=Path,
        help="Raw H.265 file for --source file.",
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

    if args.copy_original and args.source != "file":
        parser.error("--copy-original can only be used with --source file")

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

    camera = (
        start_generated_camera(args)
        if args.source == "generated"
        else start_file_camera(args)
    )

    if camera.stdout is None:
        raise RuntimeError("Could not read FFmpeg output.")

    player = None if args.no_display else start_player()

    gross_bps = args.sample_rate / args.samples_per_bit

    print("\n========== LIVE CAMERA SIMULATION ==========")
    print(f"Source:              {args.source}")

    if args.source == "file" and args.copy_original:
        print("Mode:                original HEVC stream copy")
        print(f"Input frame rate:    {args.input_fps} fps")
        print("Duration:            until the original file ends")
        print("Video conversion:    disabled")
    else:
        print(f"Duration:            {args.duration} s")
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
    print(f"TX hold frames:      {args.tx_hold_frames:.2f}")
    print(f"Soft reset after:    {args.soft_reset_after} failed attempt(s)")
    print(f"Strong resync after: {args.hard_reset_after} failed attempt(s)")
    print(f"Periodic resync:     every {args.periodic_reset_packets} packet(s)")
    print("Packet padding:      balanced 0xAA/0x55")
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

    start_time = time.perf_counter()

    try:
        with (
            args.tx_save.open("wb") as tx_file,
            args.rx_save.open("wb") as rx_file,
        ):
            while True:
                payload = read_stream_chunk(camera.stdout, args.payload_size)

                if not payload:
                    break

                tx_file.write(payload)

                if args.software_only:
                    received_payload = payload
                    metric = 1.0

                else:
                    if (
                        args.periodic_reset_packets > 0
                        and sequence > 0
                        and sequence % args.periodic_reset_packets == 0
                    ):
                        soft_resyncs += 1
                        sdr = inplace_radio_resync(
                            sdr,
                            args,
                            rx_buffer_size,
                            reason=(
                                f"periodic maintenance before packet "
                                f"{sequence}"
                            ),
                            stronger=False,
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

                    for attempt in range(1, args.retries + 1):
                        total_attempts += 1

                        # Recreating the RX buffer every packet is safer but
                        # expensive. Fast mode relies on expected-sequence
                        # synchronization and only flushes when requested.
                        if (
                            args.flush_rx_every_packet
                            and hasattr(sdr, "rx_destroy_buffer")
                        ):
                            try:
                                sdr.rx_destroy_buffer()
                            except Exception:
                                pass

                        try:
                            sdr.tx_cyclic_buffer = True
                            sdr.tx(iq)

                            frame_time = (
                                len(iq) / args.sample_rate
                            )
                            hold_time = max(
                                args.minimum_tx_hold,
                                frame_time * args.tx_hold_frames,
                            )

                            if sequence == 0:
                                hold_time += args.startup_extra_hold

                            time.sleep(hold_time)

                            # Keep the new TX packet active and read several
                            # buffers. Early buffers may still contain the
                            # previous cyclic packet.
                            last_error = None

                            for _ in range(args.rx_warmup_captures):
                                sdr.rx()
                                total_rx_captures += 1

                            for capture_number in range(1, args.captures_per_attempt + 1):
                                rx_samples = sdr.rx()
                                total_rx_captures += 1

                                try:
                                    fields, metric = recover_packet(
                                        rx_samples,
                                        payload_size=args.payload_size,
                                        samples_per_bit=args.samples_per_bit,
                                        expected_sequence=sequence,
                                        candidates_per_phase=args.sync_candidates,
                                    )

                                    candidate_payload = fields["payload"]

                                    # In this one-Pluto stop-and-wait loopback,
                                    # the transmitter still has the source
                                    # payload. Do not accept stale or otherwise
                                    # incorrect bytes even if a header/CRC
                                    # happens to pass.
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

                            # Keep the current cyclic TX waveform active and
                            # rebuild only the RX buffer. This avoids another
                            # TX upload when the failure was caused by stale or
                            # badly aligned host RX data.
                            if args.fresh_rx_captures > 0:
                                rx_only_refreshes += 1
                                refresh_rx_buffer(
                                    sdr,
                                    pause=args.fresh_rx_pause,
                                )

                                fresh_error = last_error

                                for fresh_capture in range(
                                    1,
                                    args.fresh_rx_captures + 1,
                                ):
                                    rx_samples = sdr.rx()
                                    total_rx_captures += 1

                                    try:
                                        fields, metric = recover_packet(
                                            rx_samples,
                                            payload_size=args.payload_size,
                                            samples_per_bit=args.samples_per_bit,
                                            expected_sequence=sequence,
                                            candidates_per_phase=args.sync_candidates,
                                        )

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

                        finally:
                            try:
                                sdr.tx_destroy_buffer()
                            except Exception:
                                pass

                        if success:
                            break

                        if (
                            args.hard_reset_after > 0
                            and attempt % args.hard_reset_after == 0
                            and attempt < args.retries
                        ):
                            strong_resyncs += 1
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
                            continue

                        if (
                            args.soft_reset_after > 0
                            and attempt % args.soft_reset_after == 0
                            and attempt < args.retries
                        ):
                            soft_resyncs += 1
                            sdr = inplace_radio_resync(
                                sdr,
                                args,
                                rx_buffer_size,
                                reason=(
                                    f"packet {sequence} failed "
                                    f"{attempt} consecutive attempts"
                                ),
                                stronger=False,
                            )

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

                rx_file.write(received_payload)
                recovered_bytes += len(received_payload)

                if (
                    player is not None
                    and player.stdin is not None
                ):
                    try:
                        player.stdin.write(received_payload)
                        player.stdin.flush()
                    except BrokenPipeError:
                        print("FFplay was closed.")
                        player = None

                elapsed = time.perf_counter() - start_time
                useful_rate = (
                    recovered_bytes * 8 / elapsed
                    if elapsed > 0
                    else 0
                )

                print(
                    f"RX packet={sequence}, "
                    f"bytes={len(received_payload)}, "
                    f"sync={metric:.3f}, "
                    f"rate={useful_rate:,.0f} bit/s"
                )

                sequence += 1

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        release_pluto(sdr)

        if camera.poll() is None:
            camera.terminate()

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

    if args.tx_save.exists() and args.rx_save.exists():
        tx_hash = sha256_file(args.tx_save)
        rx_hash = sha256_file(args.rx_save)

        print(f"TX SHA-256:         {tx_hash}")
        print(f"RX SHA-256:         {rx_hash}")

        if tx_hash == rx_hash:
            print("RESULT: TX and RX H.265 streams are identical.")
        else:
            print("RESULT: TX and RX streams are different.")


if __name__ == "__main__":
    main()
