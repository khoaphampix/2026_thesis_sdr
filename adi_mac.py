#!/usr/bin/env python3
"""
adi_mac.py

Very simple two-Pluto RF connection test — RECEIVER.

Run this on the Mac connected to the RX Pluto. It searches for the known tone
transmitted by adi_win.py and prints RF LINK CONNECTED only after the tone is
detected repeatedly with sufficient SNR.
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
        "simple_rx_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
        + ".log"
    )

    logger = logging.getLogger("simple_pluto_rx")
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


def detect_tone(
    samples: np.ndarray,
    sample_rate: int,
    expected_offset: float,
    search_tolerance: float,
) -> dict[str, float]:
    """
    Find the strongest FFT bin near the expected test tone.

    Returns peak frequency, frequency error, peak/noise ratio and approximate
    time-domain RMS level.
    """
    values = np.asarray(samples, dtype=np.complex64)

    if values.size < 1_024:
        raise ValueError("RX buffer is too short.")

    values = values - np.mean(values)
    window = np.hanning(values.size).astype(np.float32)
    spectrum = np.fft.fftshift(
        np.fft.fft(values * window)
    )
    power = np.abs(spectrum) ** 2
    frequencies = np.fft.fftshift(
        np.fft.fftfreq(values.size, d=1.0 / sample_rate)
    )

    search_mask = (
        np.abs(frequencies - expected_offset)
        <= search_tolerance
    )

    if not np.any(search_mask):
        raise ValueError("Tone search window contains no FFT bins.")

    search_indexes = np.flatnonzero(search_mask)
    local_index = int(
        np.argmax(power[search_mask])
    )
    peak_index = int(search_indexes[local_index])

    peak_frequency = float(frequencies[peak_index])
    peak_power = float(power[peak_index])

    # Estimate the noise floor away from the detected tone and DC.
    exclusion_bins = max(
        8,
        int(round(values.size * 10_000 / sample_rate)),
    )
    noise_mask = np.ones(values.size, dtype=bool)

    low = max(0, peak_index - exclusion_bins)
    high = min(values.size, peak_index + exclusion_bins + 1)
    noise_mask[low:high] = False

    dc_mask = np.abs(frequencies) < 25_000
    noise_mask[dc_mask] = False

    noise_values = power[noise_mask]
    noise_power = float(
        np.median(noise_values)
        if noise_values.size
        else 1e-12
    )

    snr_db = 10.0 * np.log10(
        max(peak_power, 1e-20)
        / max(noise_power, 1e-20)
    )

    rms = float(
        np.sqrt(
            np.mean(
                (
                    np.real(values).astype(np.float64) ** 2
                    + np.imag(values).astype(np.float64) ** 2
                )
                / 2.0
            )
        )
    )
    rms_dbfs = 20.0 * np.log10(
        max(rms / 2048.0, 1e-12)
    )

    return {
        "peak_frequency": peak_frequency,
        "frequency_error": peak_frequency - expected_offset,
        "snr_db": float(snr_db),
        "rms": rms,
        "rms_dbfs": float(rms_dbfs),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect the simple PlutoSDR TX test tone."
    )
    parser.add_argument("--uri", default="usb:")
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
        "--tone-offset",
        type=int,
        default=200_000,
    )
    parser.add_argument(
        "--rx-gain",
        type=float,
        default=30.0,
    )
    parser.add_argument(
        "--rx-buffer-size",
        type=int,
        default=65_536,
    )
    parser.add_argument(
        "--search-tolerance",
        type=int,
        default=80_000,
        help=(
            "Allowed tone displacement caused by two independent "
            "Pluto oscillators."
        ),
    )
    parser.add_argument(
        "--snr-threshold",
        type=float,
        default=18.0,
        help="Minimum FFT peak-to-noise ratio for one hit.",
    )
    parser.add_argument(
        "--required-hits",
        type=int,
        default=3,
        help="Consecutive tone detections required before CONNECTED.",
    )
    parser.add_argument(
        "--link-timeout",
        type=float,
        default=2.0,
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

    if args.rx_buffer_size < 1_024:
        parser.error("--rx-buffer-size must be at least 1024")
    if args.required_hits < 1:
        parser.error("--required-hits must be at least 1")
    if args.search_tolerance <= 0:
        parser.error("--search-tolerance must be positive")

    logger, log_path = create_logger(args.log_dir)

    logger.info("========== SIMPLE PLUTO RX TEST ==========")
    logger.info("Log file: %s", log_path)
    logger.info("Opening RX Pluto at URI %s", args.uri)

    sdr = None
    connected = False
    consecutive_hits = 0
    last_hit_time: float | None = None
    last_heartbeat = 0.0
    buffer_count = 0
    best_snr_seen = -999.0

    try:
        sdr = adi.Pluto(uri=args.uri)

        sdr.sample_rate = int(args.sample_rate)
        sdr.rx_lo = int(args.frequency)
        sdr.rx_rf_bandwidth = int(args.sample_rate)
        sdr.gain_control_mode_chan0 = "manual"
        sdr.rx_hardwaregain_chan0 = float(args.rx_gain)
        sdr.rx_enabled_channels = [0]
        sdr.rx_buffer_size = int(args.rx_buffer_size)

        logger.info("USB PLUTO CONNECTED")
        logger.info("Centre frequency: %s Hz", f"{args.frequency:,}")
        logger.info("Sample rate: %s sample/s", f"{args.sample_rate:,}")
        logger.info("Expected tone offset: %+d Hz", args.tone_offset)
        logger.info(
            "Search window: %+d Hz around the expected tone",
            args.search_tolerance,
        )
        logger.info("Required SNR: %.1f dB", args.snr_threshold)
        logger.info("RX gain: %.1f dB", args.rx_gain)

        logger.info("Taking two warm-up RX buffers...")
        for _ in range(2):
            warmup = sdr.rx()
            logger.info(
                "Warm-up captured %d complex samples",
                len(warmup),
            )

        logger.info("========== RX SEARCH ACTIVE ==========")
        logger.info(
            "Now start adi_win.py on the Windows/WSL transmitter."
        )

        started = time.monotonic()

        while True:
            samples = sdr.rx()
            buffer_count += 1

            result = detect_tone(
                samples=samples,
                sample_rate=args.sample_rate,
                expected_offset=args.tone_offset,
                search_tolerance=args.search_tolerance,
            )

            now = time.monotonic()
            snr_db = result["snr_db"]
            best_snr_seen = max(best_snr_seen, snr_db)

            hit = (
                abs(result["frequency_error"])
                <= args.search_tolerance
                and snr_db >= args.snr_threshold
            )

            if hit:
                consecutive_hits += 1
                last_hit_time = now
            else:
                consecutive_hits = 0

            if (
                not connected
                and consecutive_hits >= args.required_hits
            ):
                connected = True
                logger.info("========== RF LINK CONNECTED ==========")
                logger.info(
                    "Known TX tone detected for %d consecutive buffers.",
                    consecutive_hits,
                )
                logger.info(
                    "Peak offset: %+.1f Hz",
                    result["peak_frequency"],
                )
                logger.info(
                    "Frequency error/CFO contribution: %+.1f Hz",
                    result["frequency_error"],
                )
                logger.info("Peak SNR: %.1f dB", snr_db)
                logger.info(
                    "This confirms RF energy from adi_win.py reached "
                    "adi_mac.py."
                )

            if (
                connected
                and last_hit_time is not None
                and now - last_hit_time > args.link_timeout
            ):
                connected = False
                consecutive_hits = 0
                logger.warning("========== RF LINK LOST ==========")
                logger.warning(
                    "The known tone has not met the threshold for %.1fs.",
                    now - last_hit_time,
                )

            if (
                last_heartbeat == 0.0
                or now - last_heartbeat
                >= args.heartbeat_seconds
            ):
                state = "CONNECTED" if connected else "SEARCHING"

                logger.info(
                    "RX HEARTBEAT state=%s elapsed=%.1fs "
                    "buffers=%d peak_offset=%+.1fHz "
                    "frequency_error=%+.1fHz snr=%.1fdB "
                    "best_snr=%.1fdB rms=%.1fADC(%.1fdBFS) "
                    "consecutive_hits=%d/%d",
                    state,
                    now - started,
                    buffer_count,
                    result["peak_frequency"],
                    result["frequency_error"],
                    snr_db,
                    best_snr_seen,
                    result["rms"],
                    result["rms_dbfs"],
                    consecutive_hits,
                    args.required_hits,
                )

                last_heartbeat = now

    except KeyboardInterrupt:
        logger.info("Stopping receiver after Ctrl+C.")

    except Exception:
        logger.exception("RX FATAL ERROR")
        return 1

    finally:
        if sdr is not None:
            try:
                sdr.rx_destroy_buffer()
            except Exception:
                pass

        logger.info("RX stopped.")
        logger.info("Experiment log: %s", log_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
