#!/usr/bin/env python3
"""Shared protocol for turbo two-Pluto windowed selective-repeat ARQ."""

from __future__ import annotations

from datetime import datetime
import hashlib
import logging
from pathlib import Path
import struct
import zlib

import numpy as np

DATA_MAGIC = b"P2F4"
ACK_MAGIC = b"P2A4"

DATA_NO_CRC_FORMAT = "!4sIIIHB"
DATA_FORMAT = "!4sIIIHBI"
DATA_HEADER_SIZE = struct.calcsize(DATA_FORMAT)

# magic, session, base_sequence, window_count, bitmap, crc32
ACK_NO_CRC_FORMAT = "!4sIIBI"
ACK_FORMAT = "!4sIIBII"
ACK_SIZE = struct.calcsize(ACK_FORMAT)

FLAG_MANIFEST = 0x01
FLAG_DATA = 0x02
FLAG_END = 0x04

# file_size, total_packets, sha256, payload_size, window_size, data_slot_us
MANIFEST_FORMAT = "!QI32sHHI"
MANIFEST_SIZE = struct.calcsize(MANIFEST_FORMAT)

PREAMBLE_BITS_COUNT = 256
_rng = np.random.default_rng(20260802)
PREAMBLE_BITS = _rng.integers(0, 2, PREAMBLE_BITS_COUNT, dtype=np.uint8)


def configure_logger(role: str, log_dir: Path) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{role}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = logging.getLogger(role)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
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
        raise ValueError("Bit count must be divisible by eight.")
    return np.packbits(bits).tobytes()


def bits_to_bpsk(bits: np.ndarray) -> np.ndarray:
    return (1.0 - 2.0 * np.asarray(bits, dtype=np.float32)).astype(np.complex64)


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


def build_data_packet(session: int, sequence: int, timestamp_ms: int,
                      flags: int, payload: bytes, payload_size: int) -> bytes:
    if len(payload) > payload_size:
        raise ValueError("Payload exceeds configured payload size.")
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
        + balanced_padding(payload_size - len(payload), sequence)
    )


def parse_data_packet(packet: bytes, payload_size: int) -> dict:
    if len(packet) != DATA_HEADER_SIZE + payload_size:
        raise ValueError("Wrong data packet size.")
    magic, session, sequence, timestamp_ms, payload_length, flags, expected_crc = struct.unpack(
        DATA_FORMAT, packet[:DATA_HEADER_SIZE]
    )
    if magic != DATA_MAGIC:
        raise ValueError("Wrong data magic.")
    if payload_length > payload_size:
        raise ValueError("Invalid payload length.")
    payload = packet[DATA_HEADER_SIZE:DATA_HEADER_SIZE + payload_length]
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


def build_ack_packet(session: int, base_sequence: int,
                     window_count: int, bitmap: int) -> bytes:
    if not 1 <= window_count <= 32:
        raise ValueError("ACK window_count must be in [1, 32].")
    mask = (1 << window_count) - 1
    bitmap &= mask
    body = struct.pack(
        ACK_NO_CRC_FORMAT,
        ACK_MAGIC,
        session & 0xFFFFFFFF,
        base_sequence & 0xFFFFFFFF,
        window_count,
        bitmap & 0xFFFFFFFF,
    )
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack("!I", crc)


def parse_ack_packet(packet: bytes) -> dict:
    if len(packet) != ACK_SIZE:
        raise ValueError("Wrong ACK packet size.")
    magic, session, base_sequence, window_count, bitmap, expected_crc = struct.unpack(
        ACK_FORMAT, packet
    )
    if magic != ACK_MAGIC:
        raise ValueError("Wrong ACK magic.")
    if not 1 <= window_count <= 32:
        raise ValueError("Invalid ACK window count.")
    body = struct.pack(
        ACK_NO_CRC_FORMAT,
        magic,
        session,
        base_sequence,
        window_count,
        bitmap,
    )
    if zlib.crc32(body) & 0xFFFFFFFF != expected_crc:
        raise ValueError("ACK CRC failed.")
    return {
        "session": session,
        "base_sequence": base_sequence,
        "window_count": window_count,
        "bitmap": bitmap,
    }


def packet_to_iq(packet: bytes, samples_per_bit: int, scale: float) -> np.ndarray:
    frame_bits = np.concatenate((PREAMBLE_BITS, bytes_to_bits(packet)))
    symbols = bits_to_bpsk(frame_bits)
    samples = np.repeat(symbols, samples_per_bit)
    guard = np.zeros(16 * samples_per_bit, dtype=np.complex64)
    return (np.concatenate((guard, samples, guard)) * scale).astype(np.complex64)


def packets_to_superframe_iq(packets: list[bytes], samples_per_bit: int,
                             scale: float) -> np.ndarray:
    if not packets:
        raise ValueError("Cannot build an empty superframe.")
    return np.concatenate([
        packet_to_iq(packet, samples_per_bit, scale)
        for packet in packets
    ]).astype(np.complex64)


def frame_sample_count(packet_size: int, samples_per_bit: int) -> int:
    return ((PREAMBLE_BITS_COUNT + packet_size * 8) * samples_per_bit
            + 32 * samples_per_bit)


def next_power_of_two(value: int) -> int:
    return 1 << max(0, value - 1).bit_length()


def destroy_tx(device) -> None:
    try:
        device.tx_destroy_buffer()
    except Exception:
        pass


def _decode_candidate(symbol_stream: np.ndarray, start: int,
                      complete_symbols: int, sync_symbols: np.ndarray,
                      correlation_value: complex, packet_size: int,
                      sample_rate: int, samples_per_bit: int, parser):
    frame = symbol_stream[start:start + complete_symbols]
    if len(frame) != complete_symbols:
        raise ValueError("Candidate frame is incomplete.")

    symbol_rate = sample_rate / samples_per_bit
    omega = float(np.angle(correlation_value))
    indexes = np.arange(complete_symbols, dtype=np.float32)
    derotated = frame * np.exp(-1j * omega * indexes).astype(np.complex64)

    received_sync = derotated[:len(sync_symbols)]
    channel = np.vdot(sync_symbols, received_sync) / np.vdot(sync_symbols, sync_symbols)
    if abs(channel) < 1e-6:
        raise ValueError("Weak channel estimate.")

    corrected = derotated / channel
    packet_bits = (np.real(corrected[PREAMBLE_BITS_COUNT:]) < 0).astype(np.uint8)
    packet = bits_to_bytes(packet_bits)
    if len(packet) != packet_size:
        raise ValueError("Recovered packet has wrong size.")

    fields = parser(packet)
    metric = float(
        abs(np.vdot(sync_symbols, received_sync))
        / np.sqrt(
            np.vdot(sync_symbols, sync_symbols).real
            * np.vdot(received_sync, received_sync).real
            + 1e-12
        )
    )
    cfo_hz = omega * symbol_rate / (2.0 * np.pi)
    return fields, metric, cfo_hz


def recover_packets(rx_samples: np.ndarray, packet_size: int,
                    samples_per_bit: int, sample_rate: int, magic: bytes,
                    parser, expected_session: int | None,
                    metric_threshold: float,
                    candidates_per_phase: int = 8) -> list[dict]:
    """Recover zero or more CRC-valid packets from one IQ capture."""
    samples = np.asarray(rx_samples, dtype=np.complex64)
    if len(samples) < 1024:
        return []
    samples = samples - np.mean(samples)

    complete_symbols = PREAMBLE_BITS_COUNT + packet_size * 8
    known = bytearray(magic)
    if expected_session is not None:
        known.extend(struct.pack("!I", expected_session))

    sync_bits = np.concatenate((PREAMBLE_BITS, bytes_to_bits(bytes(known))))
    sync_symbols = bits_to_bpsk(sync_bits)
    reference_diff = (sync_symbols[1:] * np.conj(sync_symbols[:-1])).astype(np.complex64)
    reference_energy = float(np.sum(np.abs(reference_diff) ** 2))

    decoded: dict[tuple[int, int], dict] = {}

    for phase in range(samples_per_bit):
        usable = ((len(samples) - phase) // samples_per_bit) * samples_per_bit
        if usable <= 0:
            continue
        blocks = samples[phase:phase + usable].reshape(-1, samples_per_bit)
        edge = 1 if samples_per_bit >= 4 else 0
        if edge and samples_per_bit - 2 * edge > 0:
            symbol_stream = np.mean(blocks[:, edge:-edge], axis=1)
        else:
            symbol_stream = np.mean(blocks, axis=1)
        symbol_stream = symbol_stream.astype(np.complex64)
        if len(symbol_stream) < complete_symbols:
            continue

        differential_stream = symbol_stream[1:] * np.conj(symbol_stream[:-1])
        correlation = np.correlate(differential_stream, reference_diff, mode="valid")
        window_energy = np.convolve(
            np.abs(differential_stream) ** 2,
            np.ones(len(reference_diff), dtype=np.float32),
            mode="valid",
        )
        metric = np.abs(correlation) / np.sqrt(
            window_energy * reference_energy + 1e-12
        )

        last_start = len(symbol_stream) - complete_symbols
        metric = metric[:last_start + 1]
        correlation = correlation[:last_start + 1]
        if len(metric) == 0:
            continue

        above = np.flatnonzero(metric >= metric_threshold)
        if len(above) == 0:
            continue

        strongest = above[np.argsort(metric[above])[::-1]]
        selected: list[int] = []
        separation = max(8, len(sync_symbols) // 4)

        for index in strongest:
            index = int(index)
            if any(abs(index - previous) < separation for previous in selected):
                continue
            selected.append(index)
            if len(selected) >= candidates_per_phase:
                break

        for start in selected:
            try:
                fields, known_metric, cfo_hz = _decode_candidate(
                    symbol_stream=symbol_stream,
                    start=start,
                    complete_symbols=complete_symbols,
                    sync_symbols=sync_symbols,
                    correlation_value=correlation[start],
                    packet_size=packet_size,
                    sample_rate=sample_rate,
                    samples_per_bit=samples_per_bit,
                    parser=parser,
                )
                if expected_session is not None and fields.get("session") != expected_session:
                    continue
                seq_key = int(fields.get("sequence", fields.get("base_sequence", 0)))
                key = (int(fields.get("session", 0)), seq_key)
                result = {
                    **fields,
                    "_metric": known_metric,
                    "_phase": phase,
                    "_cfo_hz": cfo_hz,
                }
                previous = decoded.get(key)
                if previous is None or known_metric > previous["_metric"]:
                    decoded[key] = result
            except Exception:
                continue

    return sorted(
        decoded.values(),
        key=lambda item: int(item.get("sequence", item.get("base_sequence", 0))),
    )


def window_bitmap(received_sequences: set[int], base_sequence: int,
                  window_count: int) -> int:
    bitmap = 0
    for offset in range(window_count):
        if base_sequence + offset in received_sequences:
            bitmap |= 1 << offset
    return bitmap


def bitmap_missing_sequences(base_sequence: int, window_count: int,
                             bitmap: int) -> list[int]:
    return [
        base_sequence + offset
        for offset in range(window_count)
        if not (bitmap & (1 << offset))
    ]
