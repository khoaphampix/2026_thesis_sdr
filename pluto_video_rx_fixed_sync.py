#!/usr/bin/env python3
"""


python3 pluto_video_rx_fixed_sync.py \
--uri "usb:" \
--frequency 915000000 \
--sample-rate 2000000 \
--samples-per-bit 4 \
--payload-size 400 \
--rx-gain 30 \
--rx-buffer-frames 4 \
--candidates-per-phase 16 \
--metric-threshold 0.40 \
--reorder-window 8 \
--gap-timeout 1.0 \
--playback-prebuffer-bytes 8000 \
--status-every 1 \
--log-dir logs \
--no-display \
--rx-save two_pluto_received.h265


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


class PacketRecoveryError(ValueError):
    """Packet decode failure carrying the strongest preamble metric."""

    def __init__(
        self,
        message: str,
        best_metric: float = 0.0,
    ):
        super().__init__(message)
        self.best_metric = float(best_metric)


def rx_signal_levels(
    samples: np.ndarray,
    full_scale: float,
) -> tuple[float, float, float]:
    """Return RMS ADC magnitude and approximate RMS/peak dBFS."""
    values = np.asarray(samples)
    real = np.real(values).astype(np.float64, copy=False)
    imag = np.imag(values).astype(np.float64, copy=False)

    component_power = np.mean((real * real + imag * imag) / 2.0)
    rms = float(np.sqrt(max(component_power, 0.0)))
    peak = float(max(
        np.max(np.abs(real), initial=0.0),
        np.max(np.abs(imag), initial=0.0),
    ))

    reference = max(float(full_scale), 1e-12)
    rms_dbfs = 20.0 * np.log10(max(rms / reference, 1e-12))
    peak_dbfs = 20.0 * np.log10(max(peak / reference, 1e-12))
    return rms, float(rms_dbfs), float(peak_dbfs)


def recover_any_packet(
    rx_samples: np.ndarray,
    payload_size: int,
    samples_per_bit: int,
    sample_rate: int,
    candidates_per_phase: int,
    metric_threshold: float,
) -> tuple[dict, float, int, float]:
    """
    Recover one valid P2V1 packet from an RX buffer.

    This two-Pluto synchronizer correlates the differential form of the
    complete known 512-symbol preamble. Differential correlation is tolerant
    of constant carrier-frequency and carrier-phase offsets, but unlike the
    previous 64-symbol repetition detector it does not falsely lock to an
    alternating payload, balanced padding, or a continuous test tone.

    Processing:
    1. Try every sample phase.
    2. Average samples into BPSK symbols.
    3. Differentially correlate against the exact known preamble.
    4. Estimate carrier-frequency offset from correlation phase.
    5. Derotate the complete packet.
    6. Estimate complex channel gain from the known preamble.
    7. Decide BPSK bits and validate magic, length and CRC.
    """
    samples = np.asarray(rx_samples, dtype=np.complex64)
    samples = samples - np.mean(samples)

    complete_symbols = frame_symbol_count(payload_size)
    symbol_rate = sample_rate / samples_per_bit

    reference_diff = (
        PREAMBLE_SYMBOLS[1:]
        * np.conj(PREAMBLE_SYMBOLS[:-1])
    ).astype(np.complex64)
    reference_energy = float(
        np.sum(np.abs(reference_diff) ** 2)
    )

    candidates: list[
        tuple[
            float,
            int,
            int,
            np.ndarray,
            complex,
        ]
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

        symbol_stream = np.mean(
            blocks,
            axis=1,
        ).astype(np.complex64)

        if len(symbol_stream) < complete_symbols:
            continue

        differential_stream = (
            symbol_stream[1:]
            * np.conj(symbol_stream[:-1])
        )

        correlation = np.correlate(
            differential_stream,
            reference_diff,
            mode="valid",
        )

        window_energy = np.convolve(
            np.abs(differential_stream) ** 2,
            np.ones(
                len(reference_diff),
                dtype=np.float32,
            ),
            mode="valid",
        )

        metric = np.abs(correlation) / np.sqrt(
            window_energy * reference_energy + 1e-12
        )

        # Keep only starts where the complete preamble + packet fits.
        last_start = len(symbol_stream) - complete_symbols
        metric = metric[:last_start + 1]
        correlation = correlation[:last_start + 1]

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
                    complex(correlation[index]),
                )
            )

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    if not candidates:
        raise PacketRecoveryError(
            "No known-preamble synchronization candidates.",
            best_metric=0.0,
        )

    best_metric = float(candidates[0][0])
    last_error: Exception | None = None

    for (
        differential_metric,
        start,
        phase,
        symbol_stream,
        correlation_value,
    ) in candidates:
        if differential_metric < metric_threshold:
            continue

        try:
            frame = symbol_stream[
                start:
                start + complete_symbols
            ]

            if len(frame) != complete_symbols:
                raise ValueError(
                    "Candidate does not contain a complete frame."
                )

            # Differential correlation phase is the per-symbol CFO rotation.
            omega = float(np.angle(correlation_value))

            indexes = np.arange(
                complete_symbols,
                dtype=np.float32,
            )
            derotation = np.exp(
                -1j * omega * indexes
            ).astype(np.complex64)
            derotated = frame * derotation

            received_preamble = derotated[
                :len(PREAMBLE_SYMBOLS)
            ]

            channel = np.mean(
                received_preamble
                * np.conj(PREAMBLE_SYMBOLS)
            )

            if abs(channel) < 1e-6:
                raise ValueError("Weak channel estimate.")

            # Confirm that the candidate is the exact known preamble.
            known_numerator = abs(
                np.vdot(
                    PREAMBLE_SYMBOLS,
                    received_preamble,
                )
            )
            known_denominator = np.sqrt(
                np.sum(np.abs(PREAMBLE_SYMBOLS) ** 2)
                * np.sum(np.abs(received_preamble) ** 2)
                + 1e-12
            )
            known_metric = float(
                known_numerator / known_denominator
            )

            if known_metric < metric_threshold:
                raise ValueError(
                    "Known preamble correlation too low: "
                    f"{known_metric:.3f}."
                )

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

            return (
                fields,
                known_metric,
                phase,
                cfo_hz,
            )

        except Exception as error:
            last_error = error

    if best_metric < metric_threshold:
        raise PacketRecoveryError(
            "Known preamble below threshold: "
            f"best={best_metric:.3f}, "
            f"required={metric_threshold:.3f}.",
            best_metric=best_metric,
        )

    raise PacketRecoveryError(
        "Known preamble candidate found but packet validation failed: "
        f"best={best_metric:.3f}, last error={last_error}.",
        best_metric=best_metric,
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
        default=30,
    )
    parser.add_argument(
        "--rx-buffer-frames",
        type=float,
        default=4.0,
    )
    parser.add_argument(
        "--candidates-per-phase",
        type=int,
        default=16,
    )
    parser.add_argument(
        "--metric-threshold",
        type=float,
        default=0.40,
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
        "--heartbeat-seconds",
        type=float,
        default=1.0,
        help=(
            "Print and log RX search/link status at this interval even when "
            "no packet is decoded. Default: 1 second."
        ),
    )
    parser.add_argument(
        "--link-timeout",
        type=float,
        default=5.0,
        help=(
            "Declare the RF protocol link lost after this many seconds "
            "without a CRC-valid packet. Default: 5 seconds."
        ),
    )
    parser.add_argument(
        "--no-link-warning-seconds",
        type=float,
        default=10.0,
        help=(
            "Print a detailed settings warning if no valid TX packet has "
            "ever been received after this time. Default: 10 seconds."
        ),
    )
    parser.add_argument(
        "--adc-full-scale",
        type=float,
        default=2048.0,
        help=(
            "ADC component full-scale reference used only for approximate "
            "dBFS diagnostics. Default: 2048."
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
    if args.heartbeat_seconds <= 0:
        parser.error("--heartbeat-seconds must be greater than zero")
    if args.link_timeout <= 0:
        parser.error("--link-timeout must be greater than zero")
    if args.no_link_warning_seconds <= 0:
        parser.error(
            "--no-link-warning-seconds must be greater than zero"
        )
    if args.adc_full_scale <= 0:
        parser.error("--adc-full-scale must be greater than zero")


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
    print("Sync detector:       differential known preamble")
    print(f"Frame samples:       {frame_samples:,}")
    print(f"RX buffer:           {rx_buffer_size:,} samples")
    print(f"RX gain:             {args.rx_gain:.1f} dB")
    print(f"Heartbeat interval:  {args.heartbeat_seconds:.1f} s")
    print(f"Link timeout:        {args.link_timeout:.1f} s")
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

    last_heartbeat_at = start_time
    last_valid_protocol_at: float | None = None
    first_valid_protocol_at: float | None = None
    link_connected = False
    no_link_warning_printed = False
    last_recovery_error = "No RX buffer decoded yet."
    strongest_metric_since_heartbeat = 0.0
    latest_rms = 0.0
    latest_rms_dbfs = -240.0
    latest_peak_dbfs = -240.0
    buffers_at_last_heartbeat = 0
    invalid_at_last_heartbeat = 0
    duplicates_at_last_heartbeat = 0

    def print_rx_heartbeat(force: bool = False) -> None:
        nonlocal last_heartbeat_at
        nonlocal link_connected
        nonlocal no_link_warning_printed
        nonlocal strongest_metric_since_heartbeat
        nonlocal buffers_at_last_heartbeat
        nonlocal invalid_at_last_heartbeat
        nonlocal duplicates_at_last_heartbeat

        now = time.monotonic()
        if not force and now - last_heartbeat_at < args.heartbeat_seconds:
            return

        elapsed = max(now - start_time, 1e-9)
        interval = max(now - last_heartbeat_at, 1e-9)
        buffers_delta = rx_buffers - buffers_at_last_heartbeat
        invalid_delta = invalid_buffers - invalid_at_last_heartbeat
        duplicate_delta = duplicate_packets - duplicates_at_last_heartbeat
        buffer_rate = buffers_delta / interval

        if last_valid_protocol_at is None:
            if strongest_metric_since_heartbeat >= args.metric_threshold:
                state = "TX_WAVEFORM_CANDIDATE"
            else:
                state = "SEARCHING"
        else:
            silent_time = now - last_valid_protocol_at
            if silent_time <= args.link_timeout:
                state = "CONNECTED"
            else:
                state = "LINK_LOST"
                if link_connected:
                    print()
                    print("========== RF LINK LOST ==========")
                    print(
                        "No CRC-valid transmitter packet for "
                        f"{silent_time:.1f} seconds."
                    )
                    print(
                        "RX hardware is still capturing samples; check TX "
                        "activity, gain and matching settings."
                    )
                    print()
                link_connected = False

        print(
            "RX HEARTBEAT "
            f"state={state}, "
            f"elapsed={elapsed:.1f}s, "
            f"buffers={rx_buffers:,}, "
            f"buffer_rate={buffer_rate:.1f}/s, "
            f"valid={valid_packets:,}, "
            f"duplicates={duplicate_packets:,}(+{duplicate_delta}), "
            f"invalid={invalid_buffers:,}(+{invalid_delta}), "
            f"rms={latest_rms:.1f} ADC ({latest_rms_dbfs:.1f} dBFS), "
            f"peak={latest_peak_dbfs:.1f} dBFS, "
            f"best_sync={strongest_metric_since_heartbeat:.3f}/"
            f"{args.metric_threshold:.3f}"
        )

        if state in ("SEARCHING", "TX_WAVEFORM_CANDIDATE"):
            print(f"RX DETAIL: {last_recovery_error}")

        if (
            last_valid_protocol_at is None
            and not no_link_warning_printed
            and elapsed >= args.no_link_warning_seconds
        ):
            no_link_warning_printed = True
            print()
            print("========== NO TX PROTOCOL DETECTED ==========")
            print(
                f"No CRC-valid P2V1 packet was received in {elapsed:.1f} "
                "seconds."
            )
            print(
                "The RX Pluto and sample capture are running, but an RF "
                "protocol connection has not been proved."
            )
            print("Check both TX and RX use exactly:")
            print(f"  frequency       {args.frequency}")
            print(f"  sample rate     {args.sample_rate}")
            print(f"  samples/bit     {args.samples_per_bit}")
            print(f"  payload size    {args.payload_size}")
            print(
                "Confirm TX prints increasing 'TX packet=' lines. For a "
                "short antenna test, try TX gain -40 dB and RX gain 30 dB."
            )
            print()

        last_heartbeat_at = now
        strongest_metric_since_heartbeat = 0.0
        buffers_at_last_heartbeat = rx_buffers
        invalid_at_last_heartbeat = invalid_buffers
        duplicates_at_last_heartbeat = duplicate_packets

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
        print("Opening RX Pluto through direct USB context...")
        device = open_rx_pluto(args, rx_buffer_size)

        print()
        print("========== USB PLUTO CONNECTED ==========")
        print(f"Context:             {args.uri}")
        print(f"Configured RX LO:    {int(device.rx_lo):,} Hz")
        print(
            f"Configured rate:     {int(device.sample_rate):,} sample/s"
        )
        print(
            f"Configured bandwidth:{int(device.rx_rf_bandwidth):,} Hz"
        )
        print(f"Gain mode:           {device.gain_control_mode_chan0}")
        print(
            f"Hardware gain:       "
            f"{float(device.rx_hardwaregain_chan0):.1f} dB"
        )
        print(
            f"Buffer size:         {int(device.rx_buffer_size):,} samples"
        )
        print("USB connection and RX configuration succeeded.")
        print()

        warmup_samples = None
        for warmup_index in range(2):
            warmup_samples = device.rx()
            print(
                f"RX warm-up capture {warmup_index + 1}/2: "
                f"{len(warmup_samples):,} complex samples"
            )

        if warmup_samples is None or len(warmup_samples) == 0:
            raise RuntimeError("RX Pluto returned an empty warm-up buffer.")

        latest_rms, latest_rms_dbfs, latest_peak_dbfs = rx_signal_levels(
            warmup_samples,
            args.adc_full_scale,
        )

        print()
        print("========== RX SAMPLE FLOW ACTIVE ==========")
        print(
            f"Captured:            {len(warmup_samples):,} complex samples"
        )
        print(
            f"Initial RMS:         {latest_rms:.1f} ADC "
            f"({latest_rms_dbfs:.1f} dBFS approximate)"
        )
        print(
            f"Initial peak:        {latest_peak_dbfs:.1f} dBFS approximate"
        )
        print(
            "The receiver is searching for a CRC-valid P2V1 transmitter "
            "packet."
        )
        print(
            "Only 'RF LINK CONNECTED' proves successful TX/RX "
            "communication."
        )
        print()

        while True:
            samples = device.rx()
            rx_buffers += 1
            latest_rms, latest_rms_dbfs, latest_peak_dbfs = rx_signal_levels(
                samples,
                args.adc_full_scale,
            )

            try:
                fields, metric, phase, cfo_hz = recover_any_packet(
                    samples,
                    payload_size=args.payload_size,
                    samples_per_bit=args.samples_per_bit,
                    sample_rate=args.sample_rate,
                    candidates_per_phase=args.candidates_per_phase,
                    metric_threshold=args.metric_threshold,
                )
            except PacketRecoveryError as error:
                invalid_buffers += 1
                strongest_metric_since_heartbeat = max(
                    strongest_metric_since_heartbeat,
                    error.best_metric,
                )
                last_recovery_error = str(error)
                flush_ready()
                print_rx_heartbeat()
                continue
            except Exception as error:
                invalid_buffers += 1
                last_recovery_error = (
                    f"Unexpected decoder error: {type(error).__name__}: "
                    f"{error}"
                )
                flush_ready()
                print_rx_heartbeat()
                continue

            strongest_metric_since_heartbeat = max(
                strongest_metric_since_heartbeat,
                metric,
            )
            last_recovery_error = "CRC-valid packet received."
            last_valid_protocol_at = time.monotonic()

            if not link_connected:
                link_connected = True
                if first_valid_protocol_at is None:
                    first_valid_protocol_at = last_valid_protocol_at

                print()
                print("========== RF LINK CONNECTED ==========")
                print(
                    "A CRC-valid P2V1 packet from the transmitter was "
                    "decoded."
                )
                print(f"TX session:          0x{fields['session']:08X}")
                print(f"First sequence:      {fields['sequence']}")
                print(f"Sync metric:         {metric:.3f}")
                print(f"Sample phase:        {phase}")
                print(f"Estimated CFO:       {cfo_hz:+.1f} Hz")
                print("TX and RX protocol connection is confirmed.")
                print()

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
                print_rx_heartbeat()
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

            print_rx_heartbeat()

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
    print(
        f"Saved file size:     "
        f"{args.rx_save.stat().st_size if args.rx_save.exists() else 0:,} "
        "bytes"
    )

    if valid_packets == 0:
        print(
            "RESULT: RX Pluto was opened and sampled, but no CRC-valid TX "
            "protocol packet was decoded."
        )
        print(
            "A zero-byte H.265 file is expected because only validated "
            "payload bytes are written."
        )
    elif total_payload_bytes == 0:
        print(
            "RESULT: Protocol packets were detected, but no ordered video "
            "payload was written."
        )
    else:
        print(
            "RESULT: TX/RX protocol link confirmed and validated video "
            "payload was written."
        )

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
