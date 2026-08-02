#!/usr/bin/env python3
"""
Reliable two-Pluto live camera transmitter.

Windows/WSL:
    RTSP camera -> FFmpeg low-rate HEVC -> stop-and-wait ARQ -> PlutoSDR

The transmitter keeps packet N active until the Mac receiver returns ACK N.
Only acknowledged HEVC bytes are written to the local TX reference file.
"""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import os
from pathlib import Path
import re
import secrets
import struct
import subprocess
import threading
import time

import adi

from pluto_rtsp_arq_common import (
    ACK_MAGIC,
    ACK_SIZE,
    FLAG_STREAM_DATA,
    FLAG_STREAM_START,
    FLAG_STREAM_STOP,
    STREAM_INFO_FORMAT,
    STREAM_STOP_FORMAT,
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


# Replace PASSWORD locally. The URL is never printed without redaction.
CAMERA_URL = (
    "rtsp://admin:cdu_2026@192.168.1.2:554/Preview_01_sub"
)


class BufferedFfmpegSource:
    """Read FFmpeg HEVC output in a bounded background queue."""

    def __init__(
        self,
        command: list[str],
        max_buffer_bytes: int,
        read_size: int,
        startup_timeout: float,
        idle_timeout: float,
        logger,
    ):
        self.max_buffer_bytes = max(4096, max_buffer_bytes)
        self.read_size = min(4096, max(256, read_size))
        self.startup_timeout = max(1.0, startup_timeout)
        self.idle_timeout = max(1.0, idle_timeout)
        self.logger = logger

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
                self.logger.warning("[FFmpeg] %s", line)

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

    def terminate(self) -> None:
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


def parse_video_size(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width = int(width_text)
        height = int(height_text)
    except (ValueError, AttributeError) as error:
        raise ValueError(
            "Video size must use WIDTHxHEIGHT, for example 256x144."
        ) from error

    if width < 16 or height < 16:
        raise ValueError("Video dimensions must be at least 16 pixels.")

    return width, height


def x265_parameters(args: argparse.Namespace) -> str:
    keyint = max(
        1,
        int(round(args.fps * args.keyint_seconds)),
    )
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


def start_rtsp_source(
    args: argparse.Namespace,
    logger,
) -> BufferedFfmpegSource:
    camera_url = args.camera_url or CAMERA_URL

    if not camera_url or "PASSWORD" in camera_url:
        raise ValueError(
            "Edit CAMERA_URL near the top of this file and replace "
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
        logger=logger,
    )


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


def transmit_and_wait_for_ack(
    device,
    args: argparse.Namespace,
    logger,
    session: int,
    sequence: int,
    packet: bytes,
    final_packet: bool,
) -> int:
    iq = packet_to_iq(
        packet,
        args.samples_per_bit,
        args.iq_scale,
    )
    last_error = "No ACK capture attempted."

    for attempt in range(1, args.retries + 1):
        destroy_tx(device)
        device.tx_cyclic_buffer = True
        device.tx(iq)

        logger.info(
            "TX packet=%d attempt=%d/%d state=DATA_ACTIVE",
            sequence,
            attempt,
            args.retries,
        )

        time.sleep(
            args.stop_airtime
            if final_packet
            else args.data_airtime
        )
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
                fields, metric, phase, cfo_hz = recover_packet(
                    rx_samples=samples,
                    packet_size=ACK_SIZE,
                    samples_per_bit=args.samples_per_bit,
                    sample_rate=args.sample_rate,
                    magic=ACK_MAGIC,
                    parser=parse_ack_packet,
                    expected_session=session,
                    expected_sequence=sequence,
                    candidates_per_phase=args.candidates_per_phase,
                    metric_threshold=args.metric_threshold,
                )

                logger.info(
                    "ACK RECEIVED packet=%d capture=%d "
                    "sync=%.3f phase=%d CFO=%+.1fHz",
                    sequence,
                    capture_number,
                    metric,
                    phase,
                    cfo_hz,
                )

                time.sleep(args.post_ack_guard)
                return attempt

            except Exception as error:
                last_error = str(error)

        logger.warning(
            "ACK TIMEOUT packet=%d attempt=%d: %s",
            sequence,
            attempt,
            last_error,
        )

    raise RuntimeError(
        f"Packet {sequence} was not acknowledged after "
        f"{args.retries} attempts."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream an RTSP camera reliably through two PlutoSDRs."
        )
    )

    parser.add_argument("--uri", default="usb:")
    parser.add_argument("--camera-url")
    parser.add_argument(
        "--rtsp-transport",
        choices=("tcp", "udp"),
        default="tcp",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Seconds to stream; 0 means until Ctrl+C.",
    )
    parser.add_argument("--video-size", default="256x144")
    parser.add_argument("--fps", type=int, default=2)
    parser.add_argument(
        "--video-bitrate",
        type=int,
        default=2,
        help="Target HEVC bitrate in kbit/s.",
    )
    parser.add_argument(
        "--keyint-seconds",
        type=float,
        default=20,
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

    parser.add_argument("--frequency", type=int, default=915_000_000)
    parser.add_argument("--sample-rate", type=int, default=2_000_000)
    parser.add_argument("--samples-per-bit", type=int, default=4)
    parser.add_argument("--payload-size", type=int, default=400)

    parser.add_argument("--tx-gain", type=float, default=-20.0)
    parser.add_argument("--rx-gain", type=float, default=30.0)
    parser.add_argument(
        "--iq-scale",
        type=float,
        default=float(2**13),
    )

    parser.add_argument("--data-airtime", type=float, default=0.20)
    parser.add_argument("--stop-airtime", type=float, default=0.50)
    parser.add_argument("--turnaround-guard", type=float, default=0.02)
    parser.add_argument("--ack-captures", type=int, default=12)
    parser.add_argument("--post-ack-guard", type=float, default=0.20)
    parser.add_argument("--retries", type=int, default=30)
    parser.add_argument("--candidates-per-phase", type=int, default=16)
    parser.add_argument("--metric-threshold", type=float, default=0.35)

    parser.add_argument(
        "--tx-save",
        type=Path,
        default=Path("two_pluto_camera_tx.h265"),
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
    parse_video_size(args.video_size)

    if args.fps < 1:
        parser.error("--fps must be at least 1")
    if args.video_bitrate < 1:
        parser.error("--video-bitrate must be at least 1")
    if args.payload_size < 64:
        parser.error("--payload-size must be at least 64")
    if args.samples_per_bit < 2:
        parser.error("--samples-per-bit must be at least 2")
    if args.duration < 0:
        parser.error("--duration cannot be negative")
    if args.data_airtime <= 0:
        parser.error("--data-airtime must be positive")
    if args.ack_captures < 1:
        parser.error("--ack-captures must be at least 1")
    if args.retries < 1:
        parser.error("--retries must be at least 1")


def run(args: argparse.Namespace) -> int:
    logger, log_path = configure_logger(
        "tx_rtsp_arq",
        args.log_dir,
    )

    width, height = parse_video_size(args.video_size)
    source = start_rtsp_source(args, logger)

    # Ensure FFmpeg is actually producing HEVC before starting the RF session.
    first_payload = source.read(args.payload_size)

    if not first_payload:
        source.terminate()
        raise RuntimeError(
            f"FFmpeg produced no HEVC data. Error: {source.reader_error}"
        )

    session = secrets.randbits(32)
    sequence = 0

    ack_frame_samples = frame_sample_count(
        ACK_SIZE,
        args.samples_per_bit,
    )
    ack_rx_buffer_size = next_power_of_two(
        max(ack_frame_samples * 4, 16_384)
    )

    info_payload = struct.pack(
        STREAM_INFO_FORMAT,
        width,
        height,
        args.fps,
        max(1, int(round(args.keyint_seconds))),
        args.video_bitrate * 1000,
    )

    logger.info("========== RELIABLE RTSP CAMERA TX ==========")
    logger.info("Log file: %s", log_path)
    logger.info("TX URI: %s", args.uri)
    logger.info("Frequency: %d Hz", args.frequency)
    logger.info("Sample rate: %d sample/s", args.sample_rate)
    logger.info("Samples per bit: %d", args.samples_per_bit)
    logger.info("Payload size: %d bytes", args.payload_size)
    logger.info("Video: %dx%d @ %d fps", width, height, args.fps)
    logger.info(
        "Target video bitrate: %d bit/s",
        args.video_bitrate * 1000,
    )
    logger.info("Keyframe interval: %.1f s", args.keyint_seconds)
    logger.info("Session: 0x%08X", session)
    logger.info("Data TX gain: %.1f dB", args.tx_gain)
    logger.info("ACK RX gain: %.1f dB", args.rx_gain)
    logger.info("TX reference file: %s", args.tx_save)
    logger.info("Start pluto_video_rx_rtsp_arq.py first.")

    device = None
    tx_file = None
    digest = hashlib.sha256()
    acknowledged_bytes = 0
    acknowledged_packets = 0
    total_attempts = 0
    started = time.monotonic()
    stop_reason = "FFmpeg source ended"

    try:
        device = open_pluto(args, ack_rx_buffer_size)
        logger.info("USB PLUTO CONNECTED")

        args.tx_save.parent.mkdir(parents=True, exist_ok=True)
        tx_file = args.tx_save.open("wb")

        start_packet = build_data_packet(
            session=session,
            sequence=0,
            timestamp_ms=0,
            flags=FLAG_STREAM_START,
            payload=info_payload,
            payload_size=args.payload_size,
        )
        attempts = transmit_and_wait_for_ack(
            device,
            args,
            logger,
            session,
            0,
            start_packet,
            final_packet=False,
        )
        total_attempts += attempts
        sequence = 1

        payload = first_payload

        while payload:
            packet = build_data_packet(
                session=session,
                sequence=sequence,
                timestamp_ms=int(
                    (time.monotonic() - started) * 1000
                ),
                flags=FLAG_STREAM_DATA,
                payload=payload,
                payload_size=args.payload_size,
            )

            attempts = transmit_and_wait_for_ack(
                device,
                args,
                logger,
                session,
                sequence,
                packet,
                final_packet=False,
            )
            total_attempts += attempts

            tx_file.write(payload)
            tx_file.flush()
            digest.update(payload)
            acknowledged_bytes += len(payload)
            acknowledged_packets += 1

            elapsed = max(time.monotonic() - started, 1e-9)
            useful_rate = acknowledged_bytes * 8 / elapsed
            source_buffer = source.buffered_bytes
            delay_estimate = (
                source_buffer * 8
                / max(args.video_bitrate * 1000, 1)
            )

            logger.info(
                "STREAM PROGRESS packet=%d bytes=%d "
                "rate=%.0fbit/s source_buffer=%dB "
                "estimated_source_delay=%.1fs attempts=%d",
                sequence,
                acknowledged_bytes,
                useful_rate,
                source_buffer,
                delay_estimate,
                attempts,
            )

            sequence = (sequence + 1) & 0xFFFFFFFF
            payload = source.read(args.payload_size)

            if not payload and source.reader_error is not None:
                stop_reason = f"source error: {source.reader_error}"

    except KeyboardInterrupt:
        stop_reason = "Ctrl+C requested"
        logger.info("Stopping live camera transmission after Ctrl+C.")

    finally:
        source.terminate()

    try:
        if device is not None:
            stop_payload = struct.pack(
                STREAM_STOP_FORMAT,
                acknowledged_bytes,
                digest.digest(),
            )
            stop_packet = build_data_packet(
                session=session,
                sequence=sequence,
                timestamp_ms=int(
                    (time.monotonic() - started) * 1000
                ),
                flags=FLAG_STREAM_STOP,
                payload=stop_payload,
                payload_size=args.payload_size,
            )

            attempts = transmit_and_wait_for_ack(
                device,
                args,
                logger,
                session,
                sequence,
                stop_packet,
                final_packet=True,
            )
            total_attempts += attempts
            logger.info(
                "STREAM STOP ACKNOWLEDGED packet=%d",
                sequence,
            )

    finally:
        if tx_file is not None:
            tx_file.close()

        if device is not None:
            destroy_tx(device)

            if hasattr(device, "rx_destroy_buffer"):
                try:
                    device.rx_destroy_buffer()
                except Exception:
                    pass

    elapsed = max(time.monotonic() - started, 1e-9)
    useful_rate = acknowledged_bytes * 8 / elapsed

    logger.info("========== RTSP TX COMPLETE ==========")
    logger.info("Stop reason: %s", stop_reason)
    logger.info("Session: 0x%08X", session)
    logger.info("Acknowledged packets: %d", acknowledged_packets)
    logger.info("Acknowledged bytes: %d", acknowledged_bytes)
    logger.info("Total TX attempts: %d", total_attempts)
    logger.info("Elapsed: %.3f s", elapsed)
    logger.info("Useful HEVC rate: %.0f bit/s", useful_rate)
    logger.info("Peak source buffer: %d bytes", source.high_water_bytes)
    logger.info("TX SHA-256: %s", digest.hexdigest())
    logger.info("Saved acknowledged stream: %s", args.tx_save)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())



"""
python3 pluto_video_tx_rtsp_arq.py \
--uri "usb:" \
--duration 60 \
--video-size 256x144 \
--fps 2 \
--video-bitrate 2 \
--keyint-seconds 20 \
--encoder-preset veryfast \
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
--retries 30 \
--tx-save two_pluto_camera_tx.h265


"""