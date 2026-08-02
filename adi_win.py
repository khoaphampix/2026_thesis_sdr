#!/usr/bin/env python3
"""
adi_win.py

Very simple two-Pluto RF connection test — TRANSMITTER.

Run this on the Windows/WSL computer connected to the TX Pluto.
It continuously transmits one complex tone at +200 kHz from the configured
centre frequency. The Mac receiver script, adi_mac.py, looks for this tone.

Start adi_mac.py first, then start this file.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import logging
from pathlib import Path
import time

import numpy as np
import adi


def create_logger(log_dir: Path) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / (
        "simple_tx_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
        + ".log"
    )

    logger = logging.getLogger("simple_pluto_tx")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(
        log_path,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)

    return logger, log_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transmit a simple test tone with PlutoSDR."
    )
    parser.add_argument("--uri", default="usb:")
    parser.add_argument(
        "--frequency",
        type=int,
        default=915_000_000,
        help="Pluto centre frequency in Hz.",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=2_000_000,
    )
    parser.add_argument(
        "--tone-offset",
        type=int,
        default=200_000,
        help="Tone offset from centre frequency in Hz.",
    )
    parser.add_argument(
        "--tx-gain",
        type=float,
        default=-40.0,
        help="TX hardware gain in dB. Start low when radios are close.",
    )
    parser.add_argument(
        "--amplitude",
        type=float,
        default=0.50,
        help="Digital amplitude from 0 to 0.9.",
    )
    parser.add_argument(
        "--waveform-samples",
        type=int,
        default=65_536,
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("logs"),
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not 0 < args.amplitude <= 0.9:
        parser.error("--amplitude must be in (0, 0.9]")
    if abs(args.tone_offset) >= args.sample_rate // 2:
        parser.error(
            "--tone-offset must be inside the Nyquist bandwidth."
        )
    if args.waveform_samples < 1_024:
        parser.error("--waveform-samples must be at least 1024")

    logger, log_path = create_logger(args.log_dir)

    logger.info("========== SIMPLE PLUTO TX TEST ==========")
    logger.info("Log file: %s", log_path)
    logger.info("Opening TX Pluto at URI %s", args.uri)

    sdr = None

    try:
        sdr = adi.Pluto(uri=args.uri)

        sdr.sample_rate = int(args.sample_rate)
        sdr.tx_lo = int(args.frequency)
        sdr.tx_rf_bandwidth = int(args.sample_rate)
        sdr.tx_hardwaregain_chan0 = float(args.tx_gain)
        sdr.tx_enabled_channels = [0]
        sdr.tx_cyclic_buffer = True

        logger.info("USB PLUTO CONNECTED")
        logger.info("Centre frequency: %s Hz", f"{args.frequency:,}")
        logger.info("Sample rate: %s sample/s", f"{args.sample_rate:,}")
        logger.info("Tone offset: %+d Hz", args.tone_offset)
        logger.info(
            "Expected receiver peak: %+d Hz relative to RX centre",
            args.tone_offset,
        )
        logger.info("TX gain: %.1f dB", args.tx_gain)

        sample_indexes = np.arange(
            args.waveform_samples,
            dtype=np.float64,
        )
        phase = (
            2.0
            * np.pi
            * args.tone_offset
            * sample_indexes
            / args.sample_rate
        )

        iq_scale = (2**14 - 1) * args.amplitude
        iq = (
            iq_scale * np.exp(1j * phase)
        ).astype(np.complex64)

        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass

        sdr.tx_cyclic_buffer = True
        sdr.tx(iq)

        logger.info("========== TX TONE ACTIVE ==========")
        logger.info(
            "The Pluto is continuously transmitting the test tone."
        )
        logger.info(
            "Start/observe adi_mac.py. Press Ctrl+C here to stop."
        )

        started = time.monotonic()

        while True:
            time.sleep(args.heartbeat_seconds)
            elapsed = time.monotonic() - started

            logger.info(
                "TX HEARTBEAT active=yes elapsed=%.1fs "
                "centre=%dHz tone_offset=%+dHz gain=%.1fdB",
                elapsed,
                args.frequency,
                args.tone_offset,
                args.tx_gain,
            )

    except KeyboardInterrupt:
        logger.info("Stopping transmitter after Ctrl+C.")

    except Exception:
        logger.exception("TX FATAL ERROR")
        return 1

    finally:
        if sdr is not None:
            try:
                sdr.tx_destroy_buffer()
            except Exception:
                pass

        logger.info("TX stopped.")
        logger.info("Experiment log: %s", log_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
