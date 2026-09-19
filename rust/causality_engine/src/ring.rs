use memmap2::MmapMut;
use std::{
    fs::OpenOptions,
    mem::size_of,
    path::Path,
    sync::atomic::{AtomicU32, AtomicU64, Ordering},
};

pub const SLOT_PAYLOAD: usize = 2048;
pub const SLOT_EMPTY: u32 = 0;
pub const SLOT_COMMITTED: u32 = 1;

#[repr(C, align(64))]
struct RingHeader {
    magic: [u8; 8],
    capacity: u64,
    write_seq: AtomicU64,
    _reserved: [u8; 40],
}

#[repr(C, align(64))]
struct RingSlot {
    sequence: AtomicU64,
    generation: AtomicU64,
    committed: AtomicU32,
    len: AtomicU32,
    crc32: AtomicU32,
    _pad: [u8; 36],
    payload: [u8; SLOT_PAYLOAD],
}

fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for &b in bytes {
        crc ^= b as u32;
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xedb8_8320 & mask);
        }
    }
    !crc
}

pub struct SharedSequenceRing {
    mmap: MmapMut,
    capacity: usize,
}

unsafe impl Send for SharedSequenceRing {}
unsafe impl Sync for SharedSequenceRing {}

impl SharedSequenceRing {
    pub fn open(path: &Path, capacity: usize) -> Result<Self, String> {
        if capacity < 2 { return Err("ring capacity must be >= 2".into()); }
        let total = size_of::<RingHeader>()
            .checked_add(capacity.checked_mul(size_of::<RingSlot>()).ok_or("ring size overflow")?)
            .ok_or("ring size overflow")?;
        let file = OpenOptions::new().create(true).read(true).write(true).open(path).map_err(|e| e.to_string())?;
        file.set_len(total as u64).map_err(|e| e.to_string())?;
        let mut mmap = unsafe { MmapMut::map_mut(&file).map_err(|e| e.to_string())? };
        let header = unsafe { &mut *(mmap.as_mut_ptr() as *mut RingHeader) };
        if header.magic != *b"ITMQRNG1" || header.capacity != capacity as u64 {
            unsafe { std::ptr::write_bytes(mmap.as_mut_ptr(), 0, mmap.len()); }
            let header = unsafe { &mut *(mmap.as_mut_ptr() as *mut RingHeader) };
            header.magic = *b"ITMQRNG1";
            header.capacity = capacity as u64;
            header.write_seq = AtomicU64::new(0);
            for i in 0..capacity {
                let slot = unsafe { &mut *Self::slot_ptr_raw(&mut mmap, i) };
                slot.sequence = AtomicU64::new(u64::MAX);
                slot.generation = AtomicU64::new(0);
                slot.committed = AtomicU32::new(SLOT_EMPTY);
                slot.len = AtomicU32::new(0);
                slot.crc32 = AtomicU32::new(0);
            }
            mmap.flush_async().ok();
        }
        Ok(Self { mmap, capacity })
    }

    fn header(&self) -> &RingHeader { unsafe { &*(self.mmap.as_ptr() as *const RingHeader) } }
    fn slot_ptr_raw(mmap: &mut MmapMut, index: usize) -> *mut RingSlot {
        unsafe { mmap.as_mut_ptr().add(size_of::<RingHeader>() + index * size_of::<RingSlot>()) as *mut RingSlot }
    }
    fn slot(&self, index: usize) -> &RingSlot {
        unsafe { &*(self.mmap.as_ptr().add(size_of::<RingHeader>() + index * size_of::<RingSlot>()) as *const RingSlot) }
    }

    /// Single-producer commit protocol. Consumers use sequence/generation/CRC to detect
    /// partial writes and overwrite. The copy into the mapped slot is the deliberate
    /// userspace boundary; ITM QUANT calls this COPY-MINIMIZED, not literally zero-copy.
    pub fn write(&mut self, payload: &[u8]) -> Result<u64, String> {
        if payload.len() > SLOT_PAYLOAD { return Err("payload exceeds ring slot".into()); }
        let seq = self.header().write_seq.fetch_add(1, Ordering::AcqRel);
        let index = (seq as usize) % self.capacity;
        let generation = seq / self.capacity as u64;
        let slot = unsafe { &mut *Self::slot_ptr_raw(&mut self.mmap, index) };
        slot.committed.store(SLOT_EMPTY, Ordering::Release);
        slot.sequence.store(seq, Ordering::Relaxed);
        slot.generation.store(generation, Ordering::Relaxed);
        slot.len.store(payload.len() as u32, Ordering::Relaxed);
        slot.payload[..payload.len()].copy_from_slice(payload);
        slot.crc32.store(crc32(payload), Ordering::Relaxed);
        slot.committed.store(SLOT_COMMITTED, Ordering::Release);
        Ok(seq)
    }

    pub fn read(&self, seq: u64) -> Result<Option<Vec<u8>>, String> {
        let newest = self.header().write_seq.load(Ordering::Acquire);
        if seq >= newest { return Ok(None); }
        if newest.saturating_sub(seq) > self.capacity as u64 { return Err("OVERRUN".into()); }
        let slot = self.slot((seq as usize) % self.capacity);
        if slot.committed.load(Ordering::Acquire) != SLOT_COMMITTED { return Err("LAGGING".into()); }
        if slot.sequence.load(Ordering::Acquire) != seq || slot.generation.load(Ordering::Acquire) != seq / self.capacity as u64 { return Err("OVERRUN".into()); }
        let len = slot.len.load(Ordering::Acquire) as usize;
        if len > SLOT_PAYLOAD { return Err("INVALID_LENGTH".into()); }
        let bytes = slot.payload[..len].to_vec();
        if crc32(&bytes) != slot.crc32.load(Ordering::Acquire) { return Err("CRC_ERROR".into()); }
        Ok(Some(bytes))
    }

    pub fn write_sequence(&self) -> u64 { self.header().write_seq.load(Ordering::Acquire) }
}
