"""Binary wire protocol shared by Rust ingest, Python Quant Core and browser bridges.

The protocol is intentionally explicit rather than relying on the in-memory layout of
Rust/C structs or Python objects.  Every integer is little-endian and every frame carries
an integrity checksum.  The payload is MessagePack for cross-language compatibility.

Header layout (54 bytes):
    magic[4] | version:u8 | priority:u8 | flags:u16 |
    event_ns:u64 | receive_ns:u64 | process_ns:u64 | source_seq:u64 |
    symbol_len:u16 | source_len:u16 | type_len:u16 | payload_len:u32 | crc32:u32

This is the boundary contract.  Bincode may be used internally by Rust for local spool
files, but it is deliberately NOT the public Python/Rust wire format because Python does
not have a canonical bincode decoder and raw repr(C) bytes are not a stable network ABI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict
import struct
import zlib
import msgpack
from .obs import note as _obs_note

MAGIC = b"ITMQ"
VERSION = 1
HEADER_FORMAT = "<4sBBHQQQQHHHII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

FLAG_REPLAY = 1 << 0
FLAG_OBSERVED = 1 << 1
FLAG_INFERRED = 1 << 2
FLAG_MODELLED = 1 << 3


@dataclass(slots=True)
class BinaryEvent:
    event_time_ns: int
    receive_time_ns: int
    process_time_ns: int
    source_seq: int
    priority: int
    symbol: str
    source: str
    event_type: str
    payload: Dict[str, Any]
    flags: int = 0

    def pack(self) -> bytes:
        symbol = self.symbol.encode("utf-8")
        source = self.source.encode("utf-8")
        event_type = self.event_type.encode("utf-8")
        payload = msgpack.packb(self.payload or {}, use_bin_type=True)
        body = symbol + source + event_type + payload
        crc = zlib.crc32(body) & 0xFFFFFFFF
        header = struct.pack(
            HEADER_FORMAT,
            MAGIC, VERSION, int(self.priority) & 0xFF, int(self.flags) & 0xFFFF,
            int(self.event_time_ns), int(self.receive_time_ns), int(self.process_time_ns),
            int(self.source_seq), len(symbol), len(source), len(event_type), len(payload), crc,
        )
        return header + body

    @classmethod
    def unpack(cls, raw: bytes | bytearray | memoryview) -> "BinaryEvent":
        view = memoryview(raw)
        if len(view) < HEADER_SIZE:
            raise ValueError("binary event shorter than ITMQ header")
        (
            magic, version, priority, flags, event_ns, receive_ns, process_ns, seq,
            symbol_len, source_len, type_len, payload_len, crc,
        ) = struct.unpack_from(HEADER_FORMAT, view, 0)
        if magic != MAGIC:
            raise ValueError("invalid ITMQ wire magic")
        if version != VERSION:
            raise ValueError(f"unsupported ITMQ wire version {version}")
        total = HEADER_SIZE + symbol_len + source_len + type_len + payload_len
        if len(view) != total:
            raise ValueError(f"invalid ITMQ frame length {len(view)} != {total}")
        body = view[HEADER_SIZE:]
        if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
            raise ValueError("ITMQ frame CRC mismatch")
        off = HEADER_SIZE
        symbol = bytes(view[off:off+symbol_len]).decode("utf-8", "replace"); off += symbol_len
        source = bytes(view[off:off+source_len]).decode("utf-8", "replace"); off += source_len
        event_type = bytes(view[off:off+type_len]).decode("utf-8", "replace"); off += type_len
        payload = msgpack.unpackb(bytes(view[off:off+payload_len]), raw=False, strict_map_key=False)
        return cls(
            event_time_ns=event_ns, receive_time_ns=receive_ns, process_time_ns=process_ns,
            source_seq=seq, priority=priority, flags=flags, symbol=symbol, source=source,
            event_type=event_type, payload=dict(payload or {}),
        )


def pack_surface_frame(*, field: str, rows: int, cols: int, values, sequence: int = 0) -> bytes:
    """Small binary frame for browser GPU upload.

    Layout: b'ITMS' + version:u8 + field_len:u8 + rows:u16 + cols:u16 + seq:u64 +
            min:f32 + max:f32 + field + contiguous little-endian float32 values.
    """
    import numpy as np
    arr = np.asarray(values, dtype="<f4").reshape(int(rows), int(cols))
    name = str(field).encode("utf-8")[:255]
    finite = arr[np.isfinite(arr)]
    lo = float(finite.min()) if finite.size else 0.0
    hi = float(finite.max()) if finite.size else 0.0
    header = struct.pack("<4sBBHHQff", b"ITMS", 1, len(name), int(rows), int(cols), int(sequence), lo, hi)
    return header + name + arr.tobytes(order="C")


def unpack_surface_frame(raw: bytes | bytearray | memoryview) -> dict:
    import numpy as np
    fmt = "<4sBBHHQff"; size = struct.calcsize(fmt)
    view = memoryview(raw)
    if len(view) < size:
        raise ValueError("surface frame shorter than header")
    magic, version, nlen, rows, cols, seq, lo, hi = struct.unpack_from(fmt, view, 0)
    if magic != b"ITMS" or version != 1:
        raise ValueError("invalid surface frame")
    count = int(rows) * int(cols)
    off = size
    need = off + int(nlen) + count * 4
    if len(view) != need:
        raise ValueError(f"invalid surface frame length {len(view)} != {need}")
    field = bytes(view[off:off+nlen]).decode("utf-8", "replace"); off += nlen
    arr = np.frombuffer(view[off:off+count*4], dtype="<f4", count=count).reshape(int(rows), int(cols))
    return {"field": field, "rows": rows, "cols": cols, "sequence": seq, "min": lo, "max": hi, "values": arr}

TICK_HEADER_FORMAT = "<4sBHIQ"  # magic, version, symbol_len, count, last_seq
TICK_HEADER_SIZE = struct.calcsize(TICK_HEADER_FORMAT)
TICK_RECORD_FORMAT = "<QdffQ"   # event_time_ns, price, size, signed_volume, seq
TICK_RECORD_SIZE = struct.calcsize(TICK_RECORD_FORMAT)


def pack_tick_batch(symbol: str, ticks: list[dict], last_seq: int = 0) -> bytes:
    """Binary SIP tick batch for browser/Wasm transport."""
    sb = str(symbol or "").upper().encode("utf-8")[:65535]
    rows = []
    import pandas as pd
    for r in ticks:
        try:
            t = pd.Timestamp(r.get("timestamp"))
            if t.tzinfo is None:
                # LivePriceStream timestamps are EC-naive; retain wall ordering and encode
                # as UTC-like epoch only for relative chart timing. Browser metadata still
                # carries the displayed local time through REST on initial load.
                t = t.tz_localize("America/Guayaquil").tz_convert("UTC")
            else:
                t = t.tz_convert("UTC")
            ns = int(t.value)
            price = float(r.get("price")); size = float(r.get("size") or 0.0)
            signed = float(r.get("signed_volume") or 0.0); seq = int(r.get("seq") or 0)
            if not (price == price):
                continue
            rows.append((ns, price, size, signed, seq))
        except Exception as _e:
            _obs_note('binary_protocol:156', _e)
            continue
    head = struct.pack(TICK_HEADER_FORMAT, b"ITMT", 1, len(sb), len(rows), int(last_seq))
    body = bytearray(sb)
    for row in rows:
        body += struct.pack(TICK_RECORD_FORMAT, *row)
    return head + body


def unpack_tick_batch(raw: bytes | bytearray | memoryview) -> dict:
    view = memoryview(raw)
    if len(view) < TICK_HEADER_SIZE:
        raise ValueError("tick frame shorter than header")
    magic, version, slen, count, last_seq = struct.unpack_from(TICK_HEADER_FORMAT, view, 0)
    if magic != b"ITMT" or version != 1:
        raise ValueError("invalid tick frame")
    need = TICK_HEADER_SIZE + int(slen) + int(count) * TICK_RECORD_SIZE
    if len(view) != need:
        raise ValueError(f"invalid tick frame length {len(view)} != {need}")
    off = TICK_HEADER_SIZE
    symbol = bytes(view[off:off+slen]).decode("utf-8", "replace"); off += slen
    rows=[]
    for _ in range(int(count)):
        ns, price, size, signed, seq = struct.unpack_from(TICK_RECORD_FORMAT, view, off); off += TICK_RECORD_SIZE
        rows.append({"event_time_ns":ns,"price":price,"size":size,"signed_volume":signed,"seq":seq})
    return {"symbol":symbol,"last_seq":last_seq,"ticks":rows}
