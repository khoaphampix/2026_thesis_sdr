#!/usr/bin/env python3
"""Shared protocol for reliable two-Pluto stop-and-wait H.265 transfer."""

from __future__ import annotations

from datetime import datetime
import hashlib
import logging
from pathlib import Path
import struct
import time
import zlib

import numpy as np

DATA_MAGIC = b"P2D2"
ACK_MAGIC = b"P2A2"

DATA_NO_CRC_FORMAT = "!4sIIIHB"
DATA_FORMAT = "!4sIIIHBI"
DATA_HEADER_SIZE = struct.calcsize(DATA_FORMAT)

ACK_NO_CRC_FORMAT = "!4sII"
ACK_FORMAT = "!4sIII"
ACK_SIZE = struct.calcsize(ACK_FORMAT)

FLAG_MANIFEST = 0x01
FLAG_DATA = 0x02
FLAG_END = 0x04

MANIFEST_FORMAT = "!QI32s"
MANIFEST_SIZE = struct.calcsize(MANIFEST_FORMAT)

PREAMBLE_BITS_COUNT = 512
_rng = np.random.default_rng(20260802)
PREAMBLE_BITS = _rng.integers(
    0, 2, PREAMBLE_BITS_COUNT, dtype=np.uint8
)


def configure_logger(role: str, log_dir: Path) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / (
        f"{role}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    logger = logging.getLogger(role)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger, path


def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def bits_to_bytes(bits: np.ndarray) -> bytes:
    bits = np.asarray(bits, dtype=np.uint8)
    if len(bits) % 8:
        raise ValueError("Bit count is not divisible by eight.")
    return np.packbits(bits).tobytes()


def bits_to_bpsk(bits: np.ndarray) -> np.ndarray:
    return (
        1.0 - 2.0 * np.asarray(bits, dtype=np.float32)
    ).astype(np.complex64)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def balanced_padding(length: int, sequence: int) -> bytes:
    if length <= 0:
        return b""

    first = 0xAA if sequence % 2 == 0 else 0x55
    second = 0x55 if first == 0xAA else 0xAA
    pattern = bytes((first, second))
    return (pattern * ((length + 1) // 2))[:length]


def build_data_packet(
    session: int,
    sequence: int,
    timestamp_ms: int,
    flags: int,
    payload: bytes,
    payload_size: int,
) -> bytes:
    if len(payload) > payload_size:
        raise ValueError("Payload is too large.")

    body = struct.pack(
        DATA_NO_CRC_FORMAT,
        DATA_MAGIC,
        session & 0xFFFFFFFF,
        sequence & 0xFFFFFFFF,
        timestamp_ms & 0xFFFFFFFF,
        len(payload),
        flags & 0xFF,
    )
    crc = zlib.crc32(body + payload) & 0xFFFFFFFF

    return (
        body
        + struct.pack("!I", crc)
        + payload
        + balanced_padding(
            payload_size - len(payload),
            sequence,
        )
    )


def parse_data_packet(packet: bytes, payload_size: int) -> dict:
    if len(packet) != DATA_HEADER_SIZE + payload_size:
        raise ValueError("Wrong data packet size.")

    (
        magic,
        session,
        sequence,
        timestamp_ms,
        payload_length,
        flags,
        expected_crc,
    ) = struct.unpack(DATA_FORMAT, packet[:DATA_HEADER_SIZE])

    if magic != DATA_MAGIC:
        raise ValueError("Wrong data magic.")
    if payload_length > payload_size:
        raise ValueError("Invalid payload length.")

    payload = packet[
        DATA_HEADER_SIZE:
        DATA_HEADER_SIZE + payload_length
    ]
    body = struct.pack(
        DATA_NO_CRC_FORMAT,
        magic,
        session,
        sequence,
        timestamp_ms,
        payload_length,
        flags,
    )
    actual_crc = zlib.crc32(body + payload) & 0xFFFFFFFF

    if actual_crc != expected_crc:
        raise ValueError("Header/payload CRC failed.")

    return {
        "session": session,
        "sequence": sequence,
        "timestamp_ms": timestamp_ms,
        "flags": flags,
        "payload": payload,
    }


def build_ack_packet(session: int, sequence: int) -> bytes:
    body = struct.pack(
        ACK_NO_CRC_FORMAT,
        ACK_MAGIC,
        session & 0xFFFFFFFF,
        sequence & 0xFFFFFFFF,
    )
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack("!I", crc)


def parse_ack_packet(packet: bytes) -> dict:
    if len(packet) != ACK_SIZE:
        raise ValueError("Wrong ACK size.")

    magic, session, sequence, expected_crc = struct.unpack(
        ACK_FORMAT,
        packet,
    )
    if magic != ACK_MAGIC:
        raise ValueError("Wrong ACK magic.")

    body = struct.pack(
        ACK_NO_CRC_FORMAT,
        magic,
        session,
        sequence,
    )
    if zlib.crc32(body) & 0xFFFFFFFF != expected_crc:
        raise ValueError("ACK CRC failed.")

    return {
        "session": session,
        "sequence": sequence,
    }


def packet_to_iq(
    packet: bytes,
    samples_per_bit: int,
    scale: float,
) -> np.ndarray:
    frame_bits = np.concatenate(
        (PREAMBLE_BITS, bytes_to_bits(packet))
    )
    symbols = bits_to_bpsk(frame_bits)
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
    packet_size: int,
    samples_per_bit: int,
) -> int:
    return (
        (PREAMBLE_BITS_COUNT + packet_size * 8)
        * samples_per_bit
        + 32 * samples_per_bit
    )


def next_power_of_two(value: int) -> int:
    return 1 << max(0, value - 1).bit_length()


def destroy_tx(device) -> None:
    try:
        device.tx_destroy_buffer()
    except Exception:
        pass


def refresh_rx(device, pause: float = 0.01) -> None:
    if hasattr(device, "rx_destroy_buffer"):
        try:
            device.rx_destroy_buffer()
        except Exception:
            pass
    if pause > 0:
        time.sleep(pause)


def recover_packet(
    rx_samples: np.ndarray,
    packet_size: int,
    samples_per_bit: int,
    sample_rate: int,
    magic: bytes,
    parser,
    expected_session: int | None,
    expected_sequence: int | None,
    candidates_per_phase: int,
    metric_threshold: float,
) -> tuple[dict, float, int, float]:
    """
    Recover one BPSK packet with independent-Pluto CFO correction.

    The differential synchronizer uses:
      preamble + magic [+ expected session + expected sequence].
    """
    samples = np.asarray(rx_samples, dtype=np.complex64)
    samples = samples - np.mean(samples)

    complete_symbols = PREAMBLE_BITS_COUNT + packet_size * 8
    symbol_rate = sample_rate / samples_per_bit

    known = bytearray(magic)
    if expected_session is not None:
        known.extend(struct.pack("!I", expected_session))
    if expected_sequence is not None:
        known.extend(struct.pack("!I", expected_sequence))

    sync_bits = np.concatenate(
        (PREAMBLE_BITS, bytes_to_bits(bytes(known)))
    )
    sync_symbols = bits_to_bpsk(sync_bits)
    sync_diff = (
        sync_symbols[1:] * np.conj(sync_symbols[:-1])
    ).astype(np.complex64)
    sync_energy = float(np.sum(np.abs(sync_diff) ** 2))

    candidates = []

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

        edge = 1 if samples_per_bit >= 4 else 0
        if edge and samples_per_bit - 2 * edge > 0:
            symbols = np.mean(blocks[:, edge:-edge], axis=1)
        else:
            symbols = np.mean(blocks, axis=1)

        symbols = symbols.astype(np.complex64)
        if len(symbols) < complete_symbols:
            continue

        diff = symbols[1:] * np.conj(symbols[:-1])
        corr = np.correlate(diff, sync_diff, mode="valid")
        energy = np.convolve(
            np.abs(diff) ** 2,
            np.ones(len(sync_diff), dtype=np.float32),
            mode="valid",
        )
        metric = np.abs(corr) / np.sqrt(
            energy * sync_energy + 1e-12
        )

        last_start = len(symbols) - complete_symbols
        metric = metric[:last_start + 1]
        corr = corr[:last_start + 1]

        if len(metric) == 0:
            continue

        count = min(
            max(1, candidates_per_phase),
            len(metric),
        )
        indexes = np.argpartition(metric, -count)[-count:]

        for index in indexes:
            candidates.append(
                (
                    float(metric[index]),
                    int(index),
                    phase,
                    symbols,
                    complex(corr[index]),
                )
            )

    candidates.sort(key=lambda item: item[0], reverse=True)

    if not candidates:
        raise ValueError("No synchronization candidates.")

    last_error = None
    best_metric = candidates[0][0]

    for metric, start, phase, symbols, corr in candidates:
        if metric < metric_threshold:
            continue

        try:
            frame = symbols[start:start + complete_symbols]
            omega = float(np.angle(corr))
            indexes = np.arange(
                complete_symbols,
                dtype=np.float32,
            )
            frame = frame * np.exp(
                -1j * omega * indexes
            ).astype(np.complex64)

            received_sync = frame[:len(sync_symbols)]
            channel = np.vdot(
                sync_symbols,
                received_sync,
            ) / np.vdot(sync_symbols, sync_symbols)

            if abs(channel) < 1e-6:
                raise ValueError("Weak channel estimate.")

            corrected = frame / channel
            packet_bits = (
                np.real(corrected[PREAMBLE_BITS_COUNT:]) < 0
            ).astype(np.uint8)
            fields = parser(bits_to_bytes(packet_bits))

            if (
                expected_session is not None
                and fields["session"] != expected_session
            ):
                raise ValueError("Unexpected session.")
            if (
                expected_sequence is not None
                and fields["sequence"] != expected_sequence
            ):
                raise ValueError("Unexpected sequence.")

            known_metric = float(
                abs(np.vdot(sync_symbols, received_sync))
                / np.sqrt(
                    np.vdot(sync_symbols, sync_symbols).real
                    * np.vdot(received_sync, received_sync).real
                    + 1e-12
                )
            )
            cfo_hz = omega * symbol_rate / (2.0 * np.pi)
            return fields, known_metric, phase, cfo_hz

        except Exception as error:
            last_error = error

    raise ValueError(
        f"No valid packet; best_sync={best_metric:.3f}; "
        f"last_error={last_error}"
    )
