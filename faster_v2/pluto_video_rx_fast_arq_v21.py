#!/usr/bin/env python3
"""High-speed exact H.265 file RX using windowed selective-repeat ARQ."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import struct
import time

import adi

from pluto_fast_arq_common_v21 import (
    DATA_HEADER_SIZE,
    DATA_MAGIC,
    FLAG_DATA,
    FLAG_MANIFEST,
    MANIFEST_FORMAT,
    MANIFEST_SIZE,
    build_ack_packet,
    configure_logger,
    destroy_tx,
    packet_to_iq,
    parse_data_packet,
    recover_packets,
    window_bitmap,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fast exact file receiver with windowed selective-repeat ARQ."
        )
    )
    parser.add_argument("--uri", default="usb:")

    parser.add_argument("--frequency", type=int, default=915_000_000)
    parser.add_argument("--sample-rate", type=int, default=4_000_000)
    parser.add_argument("--samples-per-bit", type=int, default=4)
    parser.add_argument("--payload-size", type=int, default=1000)
    parser.add_argument("--window-size", type=int, default=8)

    parser.add_argument("--rx-gain", type=float, default=30.0)
    parser.add_argument("--tx-gain", type=float, default=-20.0)
    parser.add_argument("--iq-scale", type=float, default=float(2**13))

    parser.add_argument("--rx-buffer-size", type=int, default=65_536)
    parser.add_argument("--ack-airtime", type=float, default=0.030)
    parser.add_argument("--final-ack-airtime", type=float, default=0.060)
    parser.add_argument("--turnaround-guard", type=float, default=0.002)
    parser.add_argument(
        "--ack-delay-factor",
        type=float,
        default=0.65,
        help=(
            "After the first packet from a window is seen, wait this "
            "fraction of the TX data slot before transmitting the bitmap ACK."
        ),
    )
    parser.add_argument(
        "--control-ack-delay",
        type=float,
        default=0.070,
        help="Delay before ACKing the repeated manifest control packet.",
    )
    parser.add_argument("--metric-threshold", type=float, default=0.35)
    parser.add_argument("--candidates-per-phase", type=int, default=8)

    parser.add_argument(
        "--rx-save",
        type=Path,
        default=Path("two_pluto_received_fast_exact.h265"),
    )
    parser.add_argument("--heartbeat-seconds", type=float, default=1.0)
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.payload_size < MANIFEST_SIZE:
        parser.error(f"--payload-size must be at least {MANIFEST_SIZE}")
    if not 1 <= args.window_size <= 32:
        parser.error("--window-size must be in [1, 32]")
    if args.samples_per_bit < 2:
        parser.error("--samples-per-bit must be at least 2")
    if args.rx_buffer_size < 8192:
        parser.error("--rx-buffer-size must be at least 8192")
    if args.ack_airtime <= 0 or args.final_ack_airtime <= 0:
        parser.error("ACK airtime must be positive")
    if not 0.1 <= args.ack_delay_factor <= 2.0:
        parser.error("--ack-delay-factor should be between 0.1 and 2.0")


def open_pluto(args: argparse.Namespace):
    device = adi.Pluto(uri=args.uri)
    device.sample_rate = int(args.sample_rate)

    device.rx_lo = int(args.frequency)
    device.rx_rf_bandwidth = int(args.sample_rate)
    device.gain_control_mode_chan0 = "manual"
    device.rx_hardwaregain_chan0 = float(args.rx_gain)
    device.rx_enabled_channels = [0]
    device.rx_buffer_size = int(args.rx_buffer_size)

    device.tx_lo = int(args.frequency)
    device.tx_rf_bandwidth = int(args.sample_rate)
    device.tx_hardwaregain_chan0 = float(args.tx_gain)
    device.tx_enabled_channels = [0]
    return device


def send_bitmap_ack(
    device,
    args: argparse.Namespace,
    logger,
    session: int,
    base_sequence: int,
    window_count: int,
    bitmap: int,
    final: bool,
) -> None:
    ack_packet = build_ack_packet(
        session=session,
        base_sequence=base_sequence,
        window_count=window_count,
        bitmap=bitmap,
    )
    iq = packet_to_iq(
        ack_packet,
        args.samples_per_bit,
        args.iq_scale,
    )
    airtime = args.final_ack_airtime if final else args.ack_airtime

    destroy_tx(device)
    device.tx_cyclic_buffer = True
    device.tx(iq)
    logger.info(
        "ACK TX base=%d count=%d bitmap=0x%X airtime=%.3fms",
        base_sequence,
        window_count,
        bitmap,
        airtime * 1000,
    )
    time.sleep(airtime)
    destroy_tx(device)
    time.sleep(args.turnaround_guard)


def run(args: argparse.Namespace) -> int:
    logger, log_path = configure_logger("rx_fast_arq", args.log_dir)
    data_packet_size = DATA_HEADER_SIZE + args.payload_size

    temporary_path = args.rx_save.with_suffix(args.rx_save.suffix + ".part")
    if temporary_path.exists():
        temporary_path.unlink()

    logger.info("========== STABLE-FAST WINDOWED FILE RX V2.1 ==========")
    logger.info("Log file: %s", log_path)
    logger.info("URI: %s", args.uri)
    logger.info("Frequency: %d Hz", args.frequency)
    logger.info("Sample rate: %d sample/s", args.sample_rate)
    logger.info("Samples/bit: %d", args.samples_per_bit)
    logger.info("Gross BPSK rate: %.0f bit/s", args.sample_rate / args.samples_per_bit)
    logger.info("Payload: %d bytes", args.payload_size)
    logger.info("Window: %d packets", args.window_size)
    logger.info("RX buffer: %d samples (%.3f ms)", args.rx_buffer_size,
                args.rx_buffer_size / args.sample_rate * 1000)
    logger.info("Final file: %s", args.rx_save)
    logger.info("Start this receiver before the transmitter.")

    device = None
    output = None

    active_session: int | None = None
    expected_size: int | None = None
    total_packets: int | None = None
    expected_digest: bytes | None = None
    effective_data_slot: float | None = None

    received_sequences: set[int] = set()
    received_bytes = 0
    duplicate_packets = 0
    invalid_captures = 0
    total_decoded_packets = 0

    pending_ack_base: int | None = None
    pending_ack_first_seen: float | None = None
    pending_ack_count = 0

    last_heartbeat = time.monotonic()
    started = last_heartbeat
    completed = False

    try:
        device = open_pluto(args)
        logger.info("USB PLUTO CONNECTED")

        for index in range(2):
            samples = device.rx()
            logger.info("Warm-up capture %d/2: %d samples", index + 1, len(samples))

        logger.info("RX SEARCH ACTIVE")

        while not completed:
            samples = device.rx()
            decoded = recover_packets(
                rx_samples=samples,
                packet_size=data_packet_size,
                samples_per_bit=args.samples_per_bit,
                sample_rate=args.sample_rate,
                magic=DATA_MAGIC,
                parser=lambda packet: parse_data_packet(packet, args.payload_size),
                expected_session=active_session,
                metric_threshold=args.metric_threshold,
                candidates_per_phase=args.candidates_per_phase,
            )

            if not decoded:
                invalid_captures += 1

            now = time.monotonic()

            for fields in decoded:
                session = fields["session"]
                sequence = fields["sequence"]
                flags = fields["flags"]
                payload = fields["payload"]
                total_decoded_packets += 1

                if flags & FLAG_MANIFEST:
                    if sequence != 0 or len(payload) != MANIFEST_SIZE:
                        logger.warning("Malformed manifest ignored")
                        continue

                    (
                        manifest_size,
                        manifest_total_packets,
                        manifest_digest,
                        manifest_payload_size,
                        manifest_window_size,
                        data_slot_us,
                    ) = struct.unpack(MANIFEST_FORMAT, payload)

                    if manifest_payload_size != args.payload_size:
                        raise RuntimeError(
                            f"Payload mismatch: TX={manifest_payload_size}, RX={args.payload_size}."
                        )
                    if manifest_window_size != args.window_size:
                        raise RuntimeError(
                            f"Window mismatch: TX={manifest_window_size}, RX={args.window_size}."
                        )

                    if active_session == session:
                        logger.info("DUPLICATE MANIFEST session=0x%08X", session)
                        time.sleep(args.control_ack_delay)
                        send_bitmap_ack(
                            device,
                            args,
                            logger,
                            session,
                            0,
                            1,
                            1,
                            final=False,
                        )
                        continue

                    active_session = session
                    expected_size = manifest_size
                    total_packets = manifest_total_packets
                    expected_digest = manifest_digest
                    effective_data_slot = data_slot_us / 1_000_000.0
                    received_sequences.clear()
                    received_bytes = 0
                    duplicate_packets = 0
                    pending_ack_base = None
                    pending_ack_first_seen = None

                    if output is not None:
                        output.close()
                    if temporary_path.exists():
                        temporary_path.unlink()
                    temporary_path.parent.mkdir(parents=True, exist_ok=True)
                    output = temporary_path.open("w+b")
                    output.truncate(expected_size)
                    output.flush()

                    logger.info("========== FILE SESSION STARTED ==========")
                    logger.info("Session: 0x%08X", active_session)
                    logger.info("Expected size: %d bytes", expected_size)
                    logger.info("Expected packets: %d", total_packets)
                    logger.info("Expected SHA-256: %s", expected_digest.hex())
                    logger.info("TX data slot: %.3f ms", effective_data_slot * 1000)
                    logger.info(
                        "Manifest sync=%.3f phase=%d CFO=%+.1fHz",
                        fields["_metric"],
                        fields["_phase"],
                        fields["_cfo_hz"],
                    )

                    time.sleep(args.control_ack_delay)
                    send_bitmap_ack(
                        device,
                        args,
                        logger,
                        active_session,
                        0,
                        1,
                        1,
                        final=False,
                    )
                    continue

                if active_session is None or output is None or total_packets is None:
                    continue
                if session != active_session:
                    continue
                if not (flags & FLAG_DATA):
                    continue
                if not 1 <= sequence <= total_packets:
                    logger.warning("Out-of-range sequence %d ignored", sequence)
                    continue

                if sequence in received_sequences:
                    duplicate_packets += 1
                else:
                    offset = (sequence - 1) * args.payload_size
                    output.seek(offset)
                    output.write(payload)
                    received_sequences.add(sequence)
                    received_bytes += len(payload)
                    logger.info(
                        "RX NEW seq=%d bytes=%d received=%d/%d sync=%.3f CFO=%+.1fHz",
                        sequence,
                        len(payload),
                        len(received_sequences),
                        total_packets,
                        fields["_metric"],
                        fields["_cfo_hz"],
                    )

                base = ((sequence - 1) // args.window_size) * args.window_size + 1
                count = min(args.window_size, total_packets - base + 1)

                if pending_ack_base != base:
                    pending_ack_base = base
                    pending_ack_count = count
                    pending_ack_first_seen = now
                elif pending_ack_first_seen is None:
                    pending_ack_first_seen = now

            # ACK one bitmap after the known TX data slot has almost completed.
            if (
                active_session is not None
                and effective_data_slot is not None
                and pending_ack_base is not None
                and pending_ack_first_seen is not None
            ):
                ack_delay = effective_data_slot * args.ack_delay_factor
                if now - pending_ack_first_seen >= ack_delay:
                    bitmap = window_bitmap(
                        received_sequences,
                        pending_ack_base,
                        pending_ack_count,
                    )
                    full_mask = (1 << pending_ack_count) - 1
                    final_window = (
                        total_packets is not None
                        and pending_ack_base + pending_ack_count - 1 == total_packets
                        and bitmap == full_mask
                        and len(received_sequences) == total_packets
                    )

                    send_bitmap_ack(
                        device,
                        args,
                        logger,
                        active_session,
                        pending_ack_base,
                        pending_ack_count,
                        bitmap,
                        final=final_window,
                    )

                    logger.info(
                        "WINDOW STATUS base=%d bitmap=0x%X/%X total_received=%d/%d",
                        pending_ack_base,
                        bitmap,
                        full_mask,
                        len(received_sequences),
                        total_packets,
                    )

                    pending_ack_base = None
                    pending_ack_first_seen = None
                    pending_ack_count = 0

                    if final_window:
                        completed = True
                        break

            if now - last_heartbeat >= args.heartbeat_seconds:
                elapsed = now - started
                rate = received_bytes * 8 / max(elapsed, 1e-9)
                logger.info(
                    "RX HEARTBEAT session=%s packets=%d/%s bytes=%d duplicates=%d invalid_captures=%d useful_rate=%.0fbit/s",
                    f"0x{active_session:08X}" if active_session is not None else "none",
                    len(received_sequences),
                    total_packets if total_packets is not None else "?",
                    received_bytes,
                    duplicate_packets,
                    invalid_captures,
                    rate,
                )
                last_heartbeat = now

    except KeyboardInterrupt:
        logger.info("Stopping receiver after Ctrl+C.")
        return 130

    finally:
        if output is not None:
            output.close()
        if device is not None:
            destroy_tx(device)
            if hasattr(device, "rx_destroy_buffer"):
                try:
                    device.rx_destroy_buffer()
                except Exception:
                    pass

    if not completed:
        raise RuntimeError("Transfer did not complete")
    if expected_size is None or total_packets is None or expected_digest is None:
        raise RuntimeError("Manifest information is incomplete")

    actual_size = temporary_path.stat().st_size
    actual_digest = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
    expected_digest_hex = expected_digest.hex()

    packet_match = len(received_sequences) == total_packets
    size_match = actual_size == expected_size
    hash_match = actual_digest == expected_digest_hex

    logger.info("========== STABLE-FAST RX VERIFICATION V2.1 ==========")
    logger.info("Packets: %d / %d", len(received_sequences), total_packets)
    logger.info("Received payload bytes: %d", received_bytes)
    logger.info("File bytes: %d / %d", actual_size, expected_size)
    logger.info("Duplicate packets: %d", duplicate_packets)
    logger.info("Expected SHA-256: %s", expected_digest_hex)
    logger.info("Received SHA-256: %s", actual_digest)
    logger.info("Packet count match: %s", packet_match)
    logger.info("Size match: %s", size_match)
    logger.info("Hash match: %s", hash_match)

    if not packet_match or not size_match or not hash_match:
        raise RuntimeError("Verification failed; .part file was kept")

    if args.rx_save.exists():
        args.rx_save.unlink()
    temporary_path.replace(args.rx_save)

    elapsed = time.monotonic() - started
    rate = expected_size * 8 / max(elapsed, 1e-9)
    logger.info("Final file: %s", args.rx_save)
    logger.info("Elapsed: %.3f s", elapsed)
    logger.info("Useful file rate: %.0f bit/s", rate)
    logger.info("RESULT: TX and RX files are identical.")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
