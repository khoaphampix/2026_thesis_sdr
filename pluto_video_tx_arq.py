"""
python3 pluto_video_tx_arq.py \
--uri "usb:" \
--input "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265" \
--frequency 915000000 \
--sample-rate 2000000 \
--samples-per-bit 4 \
--payload-size 400 \
--tx-gain -20 \
--rx-gain 30 \
--data-airtime 0.20 \
--turnaround-guard 0.02 \
--ack-captures 12 \
--post-ack-guard 0.20 \
--retries 30


"""

#!/usr/bin/env python3
"""Reliable Windows/WSL H.265 transmitter using stop-and-wait ARQ."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import secrets
import struct
import time

import adi

from pluto_arq_common import (
    ACK_MAGIC,
    ACK_SIZE,
    DATA_HEADER_SIZE,
    FLAG_DATA,
    FLAG_END,
    FLAG_MANIFEST,
    MANIFEST_FORMAT,
    MANIFEST_SIZE,
    build_data_packet,
    configure_logger,
    destroy_tx,
    frame_sample_count,
    next_power_of_two,
    packet_to_iq,
    parse_ack_packet,
    recover_packet,
    refresh_rx,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reliably transmit one raw H.265 file with two PlutoSDRs."
        )
    )
    parser.add_argument("--uri", default="usb:")
    parser.add_argument("--input", type=Path, required=True)

    parser.add_argument("--frequency", type=int, default=915_000_000)
    parser.add_argument("--sample-rate", type=int, default=2_000_000)
    parser.add_argument("--samples-per-bit", type=int, default=4)
    parser.add_argument("--payload-size", type=int, default=400)

    parser.add_argument(
        "--tx-gain",
        type=float,
        default=-20.0,
        help="Windows data TX gain.",
    )
    parser.add_argument(
        "--rx-gain",
        type=float,
        default=30.0,
        help="Windows gain while receiving ACKs.",
    )
    parser.add_argument(
        "--iq-scale",
        type=float,
        default=float(2**13),
    )

    parser.add_argument(
        "--data-airtime",
        type=float,
        default=0.20,
        help="Seconds each data packet stays in cyclic TX.",
    )
    parser.add_argument(
        "--turnaround-guard",
        type=float,
        default=0.02,
    )
    parser.add_argument(
        "--ack-captures",
        type=int,
        default=12,
    )
    parser.add_argument(
        "--post-ack-guard",
        type=float,
        default=0.20,
        help="Wait for Mac ACK TX to end before sending next packet.",
    )
    parser.add_argument("--retries", type=int, default=30)
    parser.add_argument(
        "--candidates-per-phase",
        type=int,
        default=16,
    )
    parser.add_argument(
        "--metric-threshold",
        type=float,
        default=0.35,
    )

    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("logs"),
    )
    return parser


def validate_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if not args.input.exists():
        parser.error(f"Input does not exist: {args.input}")
    if args.input.stat().st_size == 0:
        parser.error("Input file is empty.")
    if args.payload_size < MANIFEST_SIZE:
        parser.error(
            f"--payload-size must be at least {MANIFEST_SIZE}"
        )
    if args.samples_per_bit < 2:
        parser.error("--samples-per-bit must be at least 2")
    if args.data_airtime <= 0:
        parser.error("--data-airtime must be positive")
    if args.ack_captures < 1:
        parser.error("--ack-captures must be at least 1")
    if args.retries < 1:
        parser.error("--retries must be at least 1")


def open_pluto(
    args: argparse.Namespace,
    ack_rx_buffer_size: int,
):
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
    device.rx_buffer_size = int(ack_rx_buffer_size)

    return device


def run(args: argparse.Namespace) -> int:
    logger, log_path = configure_logger("tx_arq", args.log_dir)

    file_bytes = args.input.read_bytes()
    file_size = len(file_bytes)
    file_digest = hashlib.sha256(file_bytes).digest()

    chunks = [
        file_bytes[index:index + args.payload_size]
        for index in range(0, file_size, args.payload_size)
    ]
    total_data_packets = len(chunks)

    session = secrets.randbits(32)
    manifest = struct.pack(
        MANIFEST_FORMAT,
        file_size,
        total_data_packets,
        file_digest,
    )

    logical_packets = [(0, FLAG_MANIFEST, manifest)]

    for sequence, payload in enumerate(chunks, start=1):
        flags = FLAG_DATA
        if sequence == total_data_packets:
            flags |= FLAG_END
        logical_packets.append((sequence, flags, payload))

    ack_frame_samples = frame_sample_count(
        ACK_SIZE,
        args.samples_per_bit,
    )
    ack_rx_buffer_size = next_power_of_two(
        max(ack_frame_samples * 4, 16_384)
    )

    logger.info("========== RELIABLE TWO-PLUTO TX ==========")
    logger.info("Log file: %s", log_path)
    logger.info("TX URI: %s", args.uri)
    logger.info("Frequency: %d Hz", args.frequency)
    logger.info("Sample rate: %d sample/s", args.sample_rate)
    logger.info("Samples per bit: %d", args.samples_per_bit)
    logger.info("Payload size: %d bytes", args.payload_size)
    logger.info("Input: %s", args.input)
    logger.info("Input size: %d bytes", file_size)
    logger.info("Video packets: %d", total_data_packets)
    logger.info("Session: 0x%08X", session)
    logger.info("SHA-256: %s", file_digest.hex())
    logger.info("Data TX gain: %.1f dB", args.tx_gain)
    logger.info("ACK RX gain: %.1f dB", args.rx_gain)
    logger.info("Start pluto_video_rx_arq.py first.")

    device = None
    started = time.monotonic()
    total_attempts = 0
    retry_count = 0

    try:
        device = open_pluto(args, ack_rx_buffer_size)
        logger.info("USB PLUTO CONNECTED")

        for sequence, flags, payload in logical_packets:
            packet = build_data_packet(
                session=session,
                sequence=sequence,
                timestamp_ms=int(
                    (time.monotonic() - started) * 1000
                ),
                flags=flags,
                payload=payload,
                payload_size=args.payload_size,
            )
            iq = packet_to_iq(
                packet,
                args.samples_per_bit,
                args.iq_scale,
            )

            acknowledged = False
            last_error = "No ACK decoded."

            for attempt in range(1, args.retries + 1):
                total_attempts += 1

                destroy_tx(device)
                device.tx_cyclic_buffer = True
                device.tx(iq)

                logger.info(
                    "TX packet=%d attempt=%d/%d bytes=%d "
                    "state=DATA_ACTIVE",
                    sequence,
                    attempt,
                    args.retries,
                    len(payload),
                )

                time.sleep(args.data_airtime)
                destroy_tx(device)
                refresh_rx(
                    device,
                    pause=args.turnaround_guard,
                )

                for capture_number in range(
                    1,
                    args.ack_captures + 1,
                ):
                    samples = device.rx()

                    try:
                        (
                            fields,
                            metric,
                            phase,
                            cfo_hz,
                        ) = recover_packet(
                            rx_samples=samples,
                            packet_size=ACK_SIZE,
                            samples_per_bit=args.samples_per_bit,
                            sample_rate=args.sample_rate,
                            magic=ACK_MAGIC,
                            parser=parse_ack_packet,
                            expected_session=session,
                            expected_sequence=sequence,
                            candidates_per_phase=(
                                args.candidates_per_phase
                            ),
                            metric_threshold=(
                                args.metric_threshold
                            ),
                        )

                        acknowledged = True
                        logger.info(
                            "ACK RECEIVED packet=%d capture=%d "
                            "sync=%.3f phase=%d CFO=%+.1fHz",
                            sequence,
                            capture_number,
                            metric,
                            phase,
                            cfo_hz,
                        )
                        break

                    except Exception as error:
                        last_error = str(error)

                if acknowledged:
                    if attempt > 1:
                        retry_count += attempt - 1
                    time.sleep(args.post_ack_guard)
                    break

                logger.warning(
                    "ACK TIMEOUT packet=%d attempt=%d: %s",
                    sequence,
                    attempt,
                    last_error,
                )

            if not acknowledged:
                raise RuntimeError(
                    f"Packet {sequence} was not acknowledged after "
                    f"{args.retries} attempts."
                )

            logger.info(
                "TX PROGRESS acknowledged=%d/%d elapsed=%.1fs",
                sequence,
                total_data_packets,
                time.monotonic() - started,
            )

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
    rate = file_size * 8 / elapsed if elapsed > 0 else 0

    logger.info("========== TX COMPLETE ==========")
    logger.info("Session: 0x%08X", session)
    logger.info("File bytes: %d", file_size)
    logger.info("Video packets: %d", total_data_packets)
    logger.info("Total TX attempts: %d", total_attempts)
    logger.info("Retries used: %d", retry_count)
    logger.info("Elapsed: %.3f s", elapsed)
    logger.info("Useful file rate: %.0f bit/s", rate)
    logger.info("TX SHA-256: %s", file_digest.hex())
    logger.info(
        "RESULT: every packet was acknowledged by the Mac receiver."
    )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
