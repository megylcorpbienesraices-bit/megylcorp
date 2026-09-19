"""Safe reference semantics for a multi-consumer sequence ring.

This Python implementation is for tests/fallback/Replay semantics.  The Rust core owns
the production shared-memory implementation.  Slot sequence + generation + committed +
CRC make overwrite/partial-write detection explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import zlib

@dataclass
class RingSlot:
    sequence: int = -1
    generation: int = 0
    committed: bool = False
    payload: bytes = b""
    crc32: int = 0

class SequenceRing:
    def __init__(self, capacity: int = 1024):
        if capacity < 2: raise ValueError("capacity >= 2")
        self.capacity=int(capacity); self.slots=[RingSlot() for _ in range(self.capacity)]; self.write_seq=0
    def write(self, payload: bytes) -> int:
        seq=self.write_seq; self.write_seq+=1; i=seq%self.capacity; gen=seq//self.capacity; s=self.slots[i]
        s.committed=False; s.sequence=seq; s.generation=gen; s.payload=bytes(payload); s.crc32=zlib.crc32(s.payload)&0xffffffff; s.committed=True
        return seq
    def read(self, seq: int) -> tuple[str, Optional[bytes]]:
        if seq < max(0,self.write_seq-self.capacity): return "OVERRUN",None
        if seq >= self.write_seq: return "WAIT",None
        s=self.slots[seq%self.capacity]
        if not s.committed or s.sequence!=seq or s.generation!=seq//self.capacity: return "LAGGING",None
        if (zlib.crc32(s.payload)&0xffffffff)!=s.crc32: return "CRC_ERROR",None
        return "OK",bytes(s.payload)
