#!/usr/bin/env python3
"""High-speed exact H.265 file TX using windowed selective-repeat ARQ."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import secrets
import struct
import time

import adi

from pluto_fast_arq_common import (
    ACK_MAGIC,
    ACK_SIZE,
    DATA_HEADER_SIZE,
    FLAG_DATA,
    FLAG_END,
    FLAG_MANIFEST,
    MANIFEST_FORMAT,
    MANIFEST_SIZE,
    bitmap_missing_sequences,
    build_data_packet,
    configure_logger,
    destroy_tx,
    frame_sample_count,
    packets_to_superframe_iq,
    parse_ack_packet,
    recover_packets,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fast exact file transfer with windowed selective-repeat ARQ."
        )
    )
    parser.add_argument("--uri", default="usb:")
    parser.add_argument("--input", type=Path, required=True)

    parser.add_argument("--frequency", type=int, default=915_000_000)
    parser.add_argument("--sample-rate", type=int, default=4_000_000)
    parser.add_argument("--samples-per-bit", type=int, default=4)
    parser.add_argument("--payload-size", type=int, default=800)
    parser.add_argument("--window-size", type=int, default=8)

    parser.add_argument("--tx-gain", type=float, default=-20.0)
    parser.add_argument("--rx-gain", type=float, default=30.0)
    parser.add_argument("--iq-scale", type=float, default=float(2**13))

    parser.add_argument(
        "--data-slot",
        type=float,
        default=0.10,
        help=(
            "Minimum seconds each window burst is held cyclically. "
            "The program automatically increases it if one full window "
            "does not fit."
        ),
    )
    parser.add_argument("--control-slot", type=float, default=0.08)
    parser.add_argument("--turnaround-guard", type=float, default=0.005)
    parser.add_argument("--ack-captures", type=int, default=40)
    parser.add_argument("--ack-rx-buffer", type=int, default=16_384)
    parser.add_argument("--retries", type=int, default=20)
    parser.add_argument("--metric-threshold", type=float, default=0.35)
    parser.add_argument("--candidates-per-phase", type=int, default=8)
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.input.exists():
        parser.error(f"Input does not exist: {args.input}")
    if args.input.stat().st_size == 0:
        parser.error("Input file is empty.")
    if args.payload_size < MANIFEST_SIZE:
        parser.error(f"--payload-size must be at least {MANIFEST_SIZE}")
    if not 1 <= args.window_size <= 32:
        parser.error("--window-size must be in [1, 32]")
    if args.samples_per_bit < 2:
        parser.error("--samples-per-bit must be at least 2")
    if args.sample_rate < 1_000_000:
        parser.error("--sample-rate is unexpectedly low")
    if args.data_slot <= 0 or args.control_slot <= 0:
        parser.error("slot durations must be positive")
    if args.ack_captures < 1:
        parser.error("--ack-captures must be at least 1")
    if args.ack_rx_buffer < 4096:
        parser.error("--ack-rx-buffer must be at least 4096")
    if args.retries < 1:
        parser.error("--retries must be at least 1")


def open_pluto(args: argparse.Namespace):
    device = adi.Pluto(uri=args.uri)
    device.sample_rate = int(args.sample_rate)

    device.tx_lo = int(args.frequency)
    device.tx_rf_bandwidth = int(args.sample_rate)
    device.tx_hardwaregain_chan0 = float(args.tx_gain)
    device.tx_enabled_channels = [0]

    device.rx_lo = int(args.frequency)
    device.rx_rf_bandwidth = int(args.sample_rate)
    device.gain_control_mode_chan0 = "manual"
    device.rx_hardwaregain_chan0 = float(args.rx_gain)
    device.rx_enabled_channels = [0]
    device.rx_buffer_size = int(args.ack_rx_buffer)
    return device


def send_cyclic_burst(device, iq, slot_seconds: float) -> None:
    destroy_tx(device)
    device.tx_cyclic_buffer = True
    device.tx(iq)
    time.sleep(slot_seconds)
    destroy_tx(device)


def wait_for_bitmap_ack(
    device,
    args: argparse.Namespace,
    session: int,
    base_sequence: int,
    window_count: int,
):
    last_seen = None
    for capture_number in range(1, args.ack_captures + 1):
        samples = device.rx()
        decoded = recover_packets(
            rx_samples=samples,
            packet_size=ACK_SIZE,
            samples_per_bit=args.samples_per_bit,
            sample_rate=args.sample_rate,
            magic=ACK_MAGIC,
            parser=parse_ack_packet,
            expected_session=session,
            metric_threshold=args.metric_threshold,
            candidates_per_phase=args.candidates_per_phase,
        )

        for ack in decoded:
            last_seen = ack
            if ack["base_sequence"] != base_sequence:
                continue
            if ack["window_count"] != window_count:
                continue
            return ack, capture_number

    return last_seen, None


def transfer_control_manifest(
    device,
    args: argparse.Namespace,
    logger,
    session: int,
    manifest_payload: bytes,
) -> int:
    packet = build_data_packet(
        session=session,
        sequence=0,
        timestamp_ms=0,
        flags=FLAG_MANIFEST,
        payload=manifest_payload,
        payload_size=args.payload_size,
    )
    iq = packets_to_superframe_iq(
        [packet],
        args.samples_per_bit,
        args.iq_scale,
    )

    attempts = 0
    for attempt in range(1, args.retries + 1):
        attempts += 1
        logger.info("MANIFEST attempt=%d/%d", attempt, args.retries)
        send_cyclic_burst(device, iq, args.control_slot)
        time.sleep(args.turnaround_guard)

        ack, capture_number = wait_for_bitmap_ack(
            device=device,
            args=args,
            session=session,
            base_sequence=0,
            window_count=1,
        )
        if ack is not None and capture_number is not None and (ack["bitmap"] & 1):
            logger.info(
                "MANIFEST ACK received capture=%d sync=%.3f CFO=%+.1fHz",
                capture_number,
                ack["_metric"],
                ack["_cfo_hz"],
            )
            return attempts

        logger.warning("MANIFEST ACK timeout; retransmitting manifest")

    raise RuntimeError("Manifest was not acknowledged.")


def run(args: argparse.Namespace) -> int:
    logger, log_path = configure_logger("tx_fast_arq", args.log_dir)

    file_bytes = args.input.read_bytes()
    file_size = len(file_bytes)
    file_digest = hashlib.sha256(file_bytes).digest()
    chunks = [
        file_bytes[index:index + args.payload_size]
        for index in range(0, file_size, args.payload_size)
    ]
    total_packets = len(chunks)
    session = secrets.randbits(32)

    data_packet_size = DATA_HEADER_SIZE + args.payload_size
    one_frame_samples = frame_sample_count(data_packet_size, args.samples_per_bit)
    one_frame_seconds = one_frame_samples / args.sample_rate
    full_window_airtime = one_frame_seconds * args.window_size
    effective_data_slot = max(args.data_slot, full_window_airtime * 1.25)

    manifest_payload = struct.pack(
        MANIFEST_FORMAT,
        file_size,
        total_packets,
        file_digest,
        args.payload_size,
        args.window_size,
        int(round(effective_data_slot * 1_000_000)),
    )

    logger.info("========== FAST WINDOWED FILE TX ==========")
    logger.info("Log file: %s", log_path)
    logger.info("URI: %s", args.uri)
    logger.info("Frequency: %d Hz", args.frequency)
    logger.info("Sample rate: %d sample/s", args.sample_rate)
    logger.info("Samples/bit: %d", args.samples_per_bit)
    logger.info("Gross BPSK rate: %.0f bit/s", args.sample_rate / args.samples_per_bit)
    logger.info("Payload: %d bytes", args.payload_size)
    logger.info("Window: %d packets", args.window_size)
    logger.info("One packet RF time: %.3f ms", one_frame_seconds * 1000)
    logger.info("Full-window RF time: %.3f ms", full_window_airtime * 1000)
    logger.info("Effective data slot: %.3f ms", effective_data_slot * 1000)
    logger.info("Input: %s", args.input)
    logger.info("Input size: %d bytes", file_size)
    logger.info("Data packets: %d", total_packets)
    logger.info("Session: 0x%08X", session)
    logger.info("SHA-256: %s", file_digest.hex())

    device = None
    started = time.monotonic()
    total_bursts = 0
    total_ack_captures = 0
    retransmitted_packets = 0

    try:
        device = open_pluto(args)
        logger.info("USB PLUTO CONNECTED")

        total_bursts += transfer_control_manifest(
            device,
            args,
            logger,
            session,
            manifest_payload,
        )

        base = 1
        while base <= total_packets:
            window_count = min(args.window_size, total_packets - base + 1)
            full_mask = (1 << window_count) - 1
            acked_bitmap = 0

            for attempt in range(1, args.retries + 1):
                missing_sequences = bitmap_missing_sequences(
                    base,
                    window_count,
                    acked_bitmap,
                )
                if not missing_sequences:
                    break

                if attempt > 1:
                    retransmitted_packets += len(missing_sequences)

                packets = []
                for sequence in missing_sequences:
                    payload = chunks[sequence - 1]
                    flags = FLAG_DATA
                    if sequence == total_packets:
                        flags |= FLAG_END
                    packet = build_data_packet(
                        session=session,
                        sequence=sequence,
                        timestamp_ms=int((time.monotonic() - started) * 1000),
                        flags=flags,
                        payload=payload,
                        payload_size=args.payload_size,
                    )
                    packets.append(packet)

                iq = packets_to_superframe_iq(
                    packets,
                    args.samples_per_bit,
                    args.iq_scale,
                )

                logger.info(
                    "WINDOW TX base=%d count=%d attempt=%d/%d missing=%s burst_samples=%d",
                    base,
                    window_count,
                    attempt,
                    args.retries,
                    missing_sequences,
                    len(iq),
                )

                send_cyclic_burst(device, iq, effective_data_slot)
                total_bursts += 1
                time.sleep(args.turnaround_guard)

                ack, capture_number = wait_for_bitmap_ack(
                    device=device,
                    args=args,
                    session=session,
                    base_sequence=base,
                    window_count=window_count,
                )
                if capture_number is not None:
                    total_ack_captures += capture_number

                if ack is None or capture_number is None:
                    logger.warning(
                        "WINDOW ACK timeout base=%d; retrying missing packets",
                        base,
                    )
                    continue

                acked_bitmap |= ack["bitmap"]
                acked_bitmap &= full_mask
                remaining = bitmap_missing_sequences(
                    base,
                    window_count,
                    acked_bitmap,
                )

                logger.info(
                    "WINDOW ACK base=%d bitmap=0x%X/%X capture=%d sync=%.3f CFO=%+.1fHz remaining=%s",
                    base,
                    acked_bitmap,
                    full_mask,
                    capture_number,
                    ack["_metric"],
                    ack["_cfo_hz"],
                    remaining,
                )

                if acked_bitmap == full_mask:
                    break

            if acked_bitmap != full_mask:
                raise RuntimeError(
                    f"Window base {base} was not completed after {args.retries} attempts."
                )

            elapsed = time.monotonic() - started
            acknowledged_packets = min(base + window_count - 1, total_packets)
            acknowledged_bytes = min(
                acknowledged_packets * args.payload_size,
                file_size,
            )
            rate = acknowledged_bytes * 8 / max(elapsed, 1e-9)
            logger.info(
                "PROGRESS packets=%d/%d bytes=%d/%d elapsed=%.3fs useful_rate=%.0fbit/s",
                acknowledged_packets,
                total_packets,
                acknowledged_bytes,
                file_size,
                elapsed,
                rate,
            )
            base += window_count

    except KeyboardInterrupt:
        logger.info("Stopping transmitter after Ctrl+C.")
        return 130

    finally:
        if device is not None:
            destroy_tx(device)
            if hasattr(device, "rx_destroy_buffer"):
                try:
                    device.rx_destroy_buffer()
                except Exception:
                    pass

    elapsed = time.monotonic() - started
    useful_rate = file_size * 8 / max(elapsed, 1e-9)
    logger.info("========== FAST TX COMPLETE ==========")
    logger.info("File bytes: %d", file_size)
    logger.info("Data packets: %d", total_packets)
    logger.info("Window size: %d", args.window_size)
    logger.info("Total RF bursts: %d", total_bursts)
    logger.info("Retransmitted packet instances: %d", retransmitted_packets)
    logger.info("ACK captures used: %d", total_ack_captures)
    logger.info("Elapsed: %.3f s", elapsed)
    logger.info("Useful file rate: %.0f bit/s", useful_rate)
    logger.info("TX SHA-256: %s", file_digest.hex())
    logger.info("RESULT: all windows were selectively acknowledged.")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
