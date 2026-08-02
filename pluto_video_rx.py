#!/usr/bin/env python3
"""
pluto_video_rx.py

Two-Pluto video receiver.

Flow:
    PlutoSDR receiver -> repeated-preamble synchronization
    -> carrier-frequency-offset correction -> BPSK decisions
    -> CRC/sequence checking -> jitter/reorder buffer
    -> delayed FFplay and H.265 file

The transmitter is pluto_video_tx.py.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import shlex
import sys
import threading
from collections import deque
from pathlib import Path
import struct
import subprocess
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
# Persistent experiment logging
# ---------------------------------------------------------------------------

class _TeeStream:
    """Mirror text to the terminal and to a persistent UTF-8 log file."""

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
    """Install and later restore stdout/stderr tee logging."""

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
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        self._lock = threading.Lock()

        sys.stdout = _TeeStream(
            self._stdout,
            self._file,
            self._lock,
        )
        sys.stderr = _TeeStream(
            self._stderr,
            self._file,
            self._lock,
        )

    def close(self) -> None:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            sys.stdout = self._stdout
            sys.stderr = self._stderr
            self._file.close()


def print_log_header(run_log: RunLog, role_name: str) -> None:
    print("========== EXPERIMENT LOG ==========")
    print(f"Role:                {role_name}")
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


def bits_to_bytes(bits: np.ndarray) -> bytes:
    bits = np.asarray(bits, dtype=np.uint8)

    if len(bits) % 8:
        raise ValueError("Bit count is not divisible by eight.")

    return np.packbits(bits).tobytes()


def parse_packet(packet: bytes, payload_size: int) -> dict:
    expected_size = HEADER_SIZE + payload_size

    if len(packet) != expected_size:
        raise ValueError("Wrong packet size.")

    (
        magic,
        session,
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
        raise ValueError("Wrong magic.")
    if payload_length > payload_size:
        raise ValueError("Invalid payload length.")

    payload = packet[
        HEADER_SIZE:
        HEADER_SIZE + payload_length
    ]
    actual_crc = zlib.crc32(payload) & 0xFFFFFFFF

    if actual_crc != expected_crc:
        raise ValueError("CRC failed.")

    return {
        "session": session,
        "sequence": sequence,
        "timestamp_ms": timestamp_ms,
        "flags": flags,
        "payload": payload,
    }


def frame_symbol_count(payload_size: int) -> int:
    return (
        len(PREAMBLE_BITS)
        + (HEADER_SIZE + payload_size) * 8
    )


def next_power_of_two(value: int) -> int:
    return 1 << max(0, value - 1).bit_length()


def recover_any_packet(
    rx_samples: np.ndarray,
    payload_size: int,
    samples_per_bit: int,
    sample_rate: int,
    candidates_per_phase: int,
    metric_threshold: float,
) -> tuple[dict, float, int, float]:
    """
    Recover any valid protocol packet from an RX buffer.

    The preamble consists of eight repeated 64-symbol blocks. Detection uses
    delayed repetition correlation, whose magnitude is tolerant of carrier
    frequency offset between two independent Pluto oscillators.

    Once a candidate is found, the known preamble estimates:
    - carrier phase;
    - carrier-frequency offset;
    - complex channel gain.
    """
    samples = np.asarray(rx_samples, dtype=np.complex64)
    samples = samples - np.mean(samples)

    complete_symbols = frame_symbol_count(payload_size)
    repeat_span = (
        len(PREAMBLE_BITS) - PREAMBLE_BLOCK_BITS
    )
    symbol_rate = sample_rate / samples_per_bit

    candidates: list[
        tuple[float, int, int, np.ndarray]
    ] = []

    for phase in range(samples_per_bit):
        usable = (
            (len(samples) - phase)
            // samples_per_bit
            * samples_per_bit
        )

        if usable <= 0:
            continue

        blocks = samples[
            phase:
            phase + usable
        ].reshape(-1, samples_per_bit)
        symbol_stream = np.mean(blocks, axis=1)

        if len(symbol_stream) < complete_symbols:
            continue

        delayed_products = (
            symbol_stream[PREAMBLE_BLOCK_BITS:]
            * np.conj(
                symbol_stream[:-PREAMBLE_BLOCK_BITS]
            )
        )

        if len(delayed_products) < repeat_span:
            continue

        kernel = np.ones(repeat_span, dtype=np.float32)

        repetition = np.abs(
            np.convolve(
                delayed_products,
                kernel,
                mode="valid",
            )
        )

        energy_a = np.convolve(
            np.abs(
                symbol_stream[:-PREAMBLE_BLOCK_BITS]
            ) ** 2,
            kernel,
            mode="valid",
        )
        energy_b = np.convolve(
            np.abs(
                symbol_stream[PREAMBLE_BLOCK_BITS:]
            ) ** 2,
            kernel,
            mode="valid",
        )

        metric = repetition / np.sqrt(
            energy_a * energy_b + 1e-12
        )

        last_start = len(symbol_stream) - complete_symbols
        metric = metric[:last_start + 1]

        if len(metric) == 0:
            continue

        count = min(
            max(1, candidates_per_phase),
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
                    phase,
                    symbol_stream,
                )
            )

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    last_error: Exception | None = None

    for metric, start, phase, symbol_stream in candidates:
        if metric < metric_threshold:
            continue

        try:
            frame = symbol_stream[
                start:
                start + complete_symbols
            ]

            received_preamble = frame[
                :len(PREAMBLE_SYMBOLS)
            ]
            despread = (
                received_preamble
                * np.conj(PREAMBLE_SYMBOLS)
            )

            adjacent = (
                despread[1:]
                * np.conj(despread[:-1])
            )
            omega = float(
                np.angle(np.sum(adjacent))
            )

            indexes = np.arange(
                complete_symbols,
                dtype=np.float32,
            )
            derotation = np.exp(
                -1j * omega * indexes
            ).astype(np.complex64)
            derotated = frame * derotation

            channel = np.mean(
                derotated[:len(PREAMBLE_SYMBOLS)]
                * np.conj(PREAMBLE_SYMBOLS)
            )

            if abs(channel) < 1e-6:
                raise ValueError("Weak channel estimate.")

            corrected = derotated / channel

            packet_bits = (
                np.real(
                    corrected[len(PREAMBLE_SYMBOLS):]
                ) < 0
            ).astype(np.uint8)

            packet = bits_to_bytes(packet_bits)
            fields = parse_packet(packet, payload_size)

            cfo_hz = (
                omega * symbol_rate / (2.0 * np.pi)
            )

            return fields, metric, phase, cfo_hz

        except Exception as error:
            last_error = error

    if not candidates:
        raise ValueError("No synchronization candidates.")

    raise ValueError(
        f"No valid packet; best metric="
        f"{candidates[0][0]:.3f}; last error={last_error}"
    )


class StreamOutput:
    def __init__(
        self,
        path: Path,
        prebuffer_bytes: int,
        display: bool,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("wb")
        self.path = path
        self.prebuffer_bytes = max(0, prebuffer_bytes)
        self.display = display
        self.pending = bytearray()
        self.player: subprocess.Popen | None = None
        self.total_bytes = 0

    def _start_player(self) -> None:
        if not self.display or self.player is not None:
            return

        command = [
            "ffplay",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "hevc",
            "-i",
            "pipe:0",
        ]

        self.player = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            bufsize=0,
        )

    def write(self, payload: bytes) -> None:
        if not payload:
            return

        self.file.write(payload)
        self.file.flush()
        self.total_bytes += len(payload)

        if not self.display:
            return

        if self.player is None:
            self.pending.extend(payload)

            if len(self.pending) < self.prebuffer_bytes:
                return

            self._start_player()

            if (
                self.player is not None
                and self.player.stdin is not None
            ):
                self.player.stdin.write(self.pending)
                self.player.stdin.flush()

            self.pending.clear()
            return

        if self.player.stdin is not None:
            try:
                self.player.stdin.write(payload)
                self.player.stdin.flush()
            except BrokenPipeError:
                print("FFplay closed.")
                self.player = None

    def close(self) -> None:
        if (
            self.display
            and self.player is None
            and self.pending
        ):
            self._start_player()

            if (
                self.player is not None
                and self.player.stdin is not None
            ):
                try:
                    self.player.stdin.write(self.pending)
                    self.player.stdin.flush()
                except BrokenPipeError:
                    pass

        self.pending.clear()
        self.file.close()

        if self.player is not None:
            if self.player.stdin is not None:
                try:
                    self.player.stdin.close()
                except Exception:
                    pass

            try:
                self.player.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.player.terminate()


def open_rx_pluto(
    args: argparse.Namespace,
    rx_buffer_size: int,
):
    device = adi.Pluto(uri=args.uri)

    device.sample_rate = int(args.sample_rate)
    device.rx_lo = int(args.frequency)
    device.rx_rf_bandwidth = int(args.sample_rate)
    device.gain_control_mode_chan0 = "manual"
    device.rx_hardwaregain_chan0 = float(args.rx_gain)
    device.rx_enabled_channels = [0]
    device.rx_buffer_size = int(rx_buffer_size)

    return device


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Receive low-bitrate HEVC video with PlutoSDR #2."
    )

    parser.add_argument(
        "--uri",
        default="usb:",
        help=(
            "RX Pluto libiio context. Default: usb:. "
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
        "--rx-gain",
        type=float,
        default=20,
    )
    parser.add_argument(
        "--rx-buffer-frames",
        type=float,
        default=4.0,
    )
    parser.add_argument(
        "--candidates-per-phase",
        type=int,
        default=8,
    )
    parser.add_argument(
        "--metric-threshold",
        type=float,
        default=0.55,
    )
    parser.add_argument(
        "--reorder-window",
        type=int,
        default=8,
        help="Skip a missing sequence once this many later packets wait.",
    )
    parser.add_argument(
        "--gap-timeout",
        type=float,
        default=1.0,
        help="Seconds to wait for a missing sequence before skipping it.",
    )
    parser.add_argument(
        "--playback-prebuffer-bytes",
        type=int,
        default=8_000,
    )
    parser.add_argument(
        "--no-display",
        action="store_true",
    )
    parser.add_argument(
        "--rx-save",
        type=Path,
        default=Path("two_pluto_received.h265"),
    )
    parser.add_argument(
        "--status-every",
        type=int,
        default=1,
        help=(
            "Write a status line every N new packets. Default 1 provides "
            "a detailed experiment log."
        ),
    )

    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("logs"),
        help="Directory for timestamped logs. Default: logs.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help=(
            "Optional exact log filename. Overrides automatic timestamp "
            "naming."
        ),
    )

    return parser


def validate_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.samples_per_bit < 2:
        parser.error("--samples-per-bit must be at least 2")
    if args.payload_size < 64:
        parser.error("--payload-size must be at least 64")
    if args.rx_buffer_frames < 2:
        parser.error("--rx-buffer-frames must be at least 2")
    if args.candidates_per_phase < 1:
        parser.error("--candidates-per-phase must be at least 1")
    if not 0 < args.metric_threshold <= 1:
        parser.error("--metric-threshold must be in (0, 1]")
    if args.reorder_window < 1:
        parser.error("--reorder-window must be at least 1")
    if args.gap_timeout <= 0:
        parser.error("--gap-timeout must be greater than zero")


def run(args: argparse.Namespace) -> int:
    frame_symbols = frame_symbol_count(args.payload_size)
    frame_samples = (
        frame_symbols * args.samples_per_bit
        + 32 * args.samples_per_bit
    )
    rx_buffer_size = next_power_of_two(
        int(frame_samples * args.rx_buffer_frames)
    )

    print("========== TWO-PLUTO VIDEO RECEIVER ==========")
    print(f"RX URI:              {args.uri}")
    print(f"Frequency:           {args.frequency:,} Hz")
    print(f"Sample rate:         {args.sample_rate:,} sample/s")
    print(f"Samples per bit:     {args.samples_per_bit}")
    print(f"Payload size:        {args.payload_size} bytes")
    print(f"Frame samples:       {frame_samples:,}")
    print(f"RX buffer:           {rx_buffer_size:,} samples")
    print(f"RX gain:             {args.rx_gain:.1f} dB")
    print(
        f"Playback prebuffer:  "
        f"{args.playback_prebuffer_bytes:,} bytes"
    )
    print(f"RX file:             {args.rx_save}")
    print("Start this receiver before pluto_video_tx.py.")
    print("Press Ctrl+C to stop.\n")

    device = None
    output = StreamOutput(
        args.rx_save,
        args.playback_prebuffer_bytes,
        not args.no_display,
    )

    active_session: int | None = None
    expected_sequence: int | None = None
    pending: dict[int, dict] = {}
    gap_started_at: float | None = None

    valid_packets = 0
    duplicate_packets = 0
    skipped_packets = 0
    invalid_buffers = 0
    rx_buffers = 0
    total_payload_bytes = 0
    recent_sequences: deque[int] = deque(maxlen=4096)
    recent_set: set[int] = set()

    start_time = time.monotonic()
    cfo_history: deque[float] = deque(maxlen=50)
    metric_history: deque[float] = deque(maxlen=50)

    def remember_sequence(sequence: int) -> None:
        if sequence in recent_set:
            return

        if len(recent_sequences) == recent_sequences.maxlen:
            old = recent_sequences.popleft()
            recent_set.discard(old)

        recent_sequences.append(sequence)
        recent_set.add(sequence)

    def reset_session(fields: dict) -> None:
        nonlocal active_session
        nonlocal expected_sequence
        nonlocal pending
        nonlocal gap_started_at
        nonlocal recent_sequences
        nonlocal recent_set

        active_session = fields["session"]
        expected_sequence = fields["sequence"]
        pending = {}
        gap_started_at = None
        recent_sequences.clear()
        recent_set.clear()

        print(
            f"New TX session: 0x{active_session:08X}; "
            f"starting at sequence {expected_sequence}"
        )

    def flush_ready() -> None:
        nonlocal expected_sequence
        nonlocal total_payload_bytes
        nonlocal gap_started_at
        nonlocal skipped_packets

        if expected_sequence is None:
            return

        while True:
            if expected_sequence in pending:
                fields = pending.pop(expected_sequence)
                payload = fields["payload"]
                output.write(payload)
                total_payload_bytes += len(payload)
                remember_sequence(expected_sequence)
                expected_sequence = (
                    expected_sequence + 1
                ) & 0xFFFFFFFF
                gap_started_at = None
                continue

            if not pending:
                gap_started_at = None
                break

            later_sequences = [
                sequence
                for sequence in pending
                if sequence > expected_sequence
            ]

            if not later_sequences:
                break

            nearest = min(later_sequences)
            distance = nearest - expected_sequence
            now = time.monotonic()

            if gap_started_at is None:
                gap_started_at = now

            timeout_reached = (
                now - gap_started_at >= args.gap_timeout
            )
            window_reached = (
                distance >= args.reorder_window
                or len(pending) >= args.reorder_window
            )

            if not timeout_reached and not window_reached:
                break

            print(
                f"Skipping missing sequence {expected_sequence}; "
                f"nearest received={nearest}"
            )
            skipped_packets += 1
            expected_sequence = (
                expected_sequence + 1
            ) & 0xFFFFFFFF
            gap_started_at = now

    try:
        device = open_rx_pluto(args, rx_buffer_size)

        # Discard two initial buffers after configuring the receiver.
        for _ in range(2):
            device.rx()

        while True:
            samples = device.rx()
            rx_buffers += 1

            try:
                fields, metric, phase, cfo_hz = recover_any_packet(
                    samples,
                    payload_size=args.payload_size,
                    samples_per_bit=args.samples_per_bit,
                    sample_rate=args.sample_rate,
                    candidates_per_phase=args.candidates_per_phase,
                    metric_threshold=args.metric_threshold,
                )
            except Exception:
                invalid_buffers += 1
                flush_ready()
                continue

            if (
                active_session is None
                or fields["session"] != active_session
                or fields["flags"] & FLAG_START
                and fields["session"] != active_session
            ):
                reset_session(fields)

            sequence = fields["sequence"]

            if sequence in recent_set or sequence in pending:
                duplicate_packets += 1
                continue

            pending[sequence] = fields
            valid_packets += 1
            cfo_history.append(cfo_hz)
            metric_history.append(metric)
            flush_ready()

            if (
                args.status_every > 0
                and valid_packets % args.status_every == 0
            ):
                elapsed = time.monotonic() - start_time
                useful_rate = (
                    total_payload_bytes * 8 / elapsed
                    if elapsed > 0
                    else 0
                )
                average_cfo = (
                    sum(cfo_history) / len(cfo_history)
                    if cfo_history
                    else 0
                )
                average_metric = (
                    sum(metric_history) / len(metric_history)
                    if metric_history
                    else 0
                )

                print(
                    f"RX seq={sequence}, "
                    f"valid={valid_packets}, "
                    f"duplicates={duplicate_packets}, "
                    f"pending={len(pending)}, "
                    f"metric={metric:.3f}, "
                    f"avg_metric={average_metric:.3f}, "
                    f"CFO={cfo_hz:+.0f} Hz, "
                    f"avg_CFO={average_cfo:+.0f} Hz, "
                    f"rate={useful_rate:,.0f} bit/s"
                )

    except KeyboardInterrupt:
        print("\nStopping receiver...")

    finally:
        output.close()

        if device is not None:
            try:
                device.rx_destroy_buffer()
            except Exception:
                pass

    elapsed = time.monotonic() - start_time
    useful_rate = (
        total_payload_bytes * 8 / elapsed
        if elapsed > 0
        else 0
    )
    average_cfo = (
        sum(cfo_history) / len(cfo_history)
        if cfo_history
        else 0
    )

    print("\n========== RX RESULT ==========")
    print(
        "Session:             "
        + (
            f"0x{active_session:08X}"
            if active_session is not None
            else "none"
        )
    )
    print(f"RX buffers:          {rx_buffers:,}")
    print(f"Valid packets:       {valid_packets:,}")
    print(f"Duplicate packets:   {duplicate_packets:,}")
    print(f"Skipped packets:     {skipped_packets:,}")
    print(f"Invalid buffers:     {invalid_buffers:,}")
    print(f"Payload bytes:       {total_payload_bytes:,}")
    print(f"Useful RX rate:      {useful_rate:,.0f} bit/s")
    print(f"Average recent CFO:  {average_cfo:+.1f} Hz")
    print(f"Saved stream:        {args.rx_save}")

    return 0



def main() -> int:
    """Parse arguments, start persistent logging, and run the rx process."""
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)

    run_log = RunLog(
        role="rx",
        log_dir=args.log_dir,
        explicit_path=args.log_file,
    )

    exit_code = 1

    try:
        print_log_header(run_log, "RECEIVER")
        exit_code = run(args)
        return exit_code

    except KeyboardInterrupt:
        print()
        print("Interrupted by user.")
        return 130

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
        print(f"Exit code:           {exit_code}")
        print(f"Experiment log:      {run_log.path}")
        run_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
