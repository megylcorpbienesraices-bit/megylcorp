use serde::{Deserialize, Serialize};

pub const MAGIC: [u8; 4] = *b"ITMQ";
pub const VERSION: u8 = 1;
pub const HEADER_SIZE: usize = 54;

// Shared event priorities.  Quotes deliberately precede trades when event-time and
// source-sequence are identical so a causal classifier can consume the contemporaneous
// NBBO before the print.  A provider with an authoritative sequence still controls the
// order through source_seq, which is compared before priority.
pub const PRIORITY_QUOTE: u8 = 10;
pub const PRIORITY_TRADE: u8 = 20;
pub const PRIORITY_OPTION_QUOTE: u8 = 10;
pub const PRIORITY_OPTION_TRADE: u8 = 20;
pub const PRIORITY_SNAPSHOT: u8 = 50;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EventEnvelope {
    pub event_time_ns: u64,
    pub receive_time_ns: u64,
    pub process_time_ns: u64,
    pub source_seq: u64,
    pub priority: u8,
    pub flags: u16,
    pub symbol: String,
    pub source: String,
    pub event_type: String,
    /// MessagePack payload bytes. Provider adapters encode normalized maps once.
    pub payload: Vec<u8>,
}

// Dependency-free CRC32 (IEEE), byte-for-byte compatible with Python zlib.crc32.
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

fn u16_at(raw: &[u8], offset: usize) -> Result<u16, String> {
    let b = raw.get(offset..offset + 2).ok_or_else(|| "truncated u16".to_string())?;
    Ok(u16::from_le_bytes([b[0], b[1]]))
}

fn u32_at(raw: &[u8], offset: usize) -> Result<u32, String> {
    let b = raw.get(offset..offset + 4).ok_or_else(|| "truncated u32".to_string())?;
    Ok(u32::from_le_bytes(b.try_into().map_err(|_| "u32 decode".to_string())?))
}

fn u64_at(raw: &[u8], offset: usize) -> Result<u64, String> {
    let b = raw.get(offset..offset + 8).ok_or_else(|| "truncated u64".to_string())?;
    Ok(u64::from_le_bytes(b.try_into().map_err(|_| "u64 decode".to_string())?))
}

impl EventEnvelope {
    pub fn ordering_key(&self) -> (u64, u64, u8, &str) {
        (self.event_time_ns, self.source_seq, self.priority, self.source.as_str())
    }

    pub fn to_wire(&self) -> Result<Vec<u8>, String> {
        let symbol = self.symbol.as_bytes();
        let source = self.source.as_bytes();
        let typ = self.event_type.as_bytes();
        let payload = self.payload.as_slice();
        if symbol.len() > u16::MAX as usize || source.len() > u16::MAX as usize || typ.len() > u16::MAX as usize {
            return Err("metadata field too long".into());
        }
        if payload.len() > u32::MAX as usize {
            return Err("payload too long".into());
        }
        let body_len = symbol.len()
            .checked_add(source.len()).and_then(|n| n.checked_add(typ.len()))
            .and_then(|n| n.checked_add(payload.len())).ok_or_else(|| "frame overflow".to_string())?;
        let mut body = Vec::with_capacity(body_len);
        body.extend_from_slice(symbol);
        body.extend_from_slice(source);
        body.extend_from_slice(typ);
        body.extend_from_slice(payload);
        let checksum = crc32(&body);

        let mut out = Vec::with_capacity(HEADER_SIZE + body.len());
        out.extend_from_slice(&MAGIC);
        out.push(VERSION);
        out.push(self.priority);
        out.extend_from_slice(&self.flags.to_le_bytes());
        out.extend_from_slice(&self.event_time_ns.to_le_bytes());
        out.extend_from_slice(&self.receive_time_ns.to_le_bytes());
        out.extend_from_slice(&self.process_time_ns.to_le_bytes());
        out.extend_from_slice(&self.source_seq.to_le_bytes());
        out.extend_from_slice(&(symbol.len() as u16).to_le_bytes());
        out.extend_from_slice(&(source.len() as u16).to_le_bytes());
        out.extend_from_slice(&(typ.len() as u16).to_le_bytes());
        out.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        out.extend_from_slice(&checksum.to_le_bytes());
        debug_assert_eq!(out.len(), HEADER_SIZE);
        out.extend_from_slice(&body);
        Ok(out)
    }

    pub fn from_wire(raw: &[u8]) -> Result<Self, String> {
        if raw.len() < HEADER_SIZE {
            return Err("frame shorter than ITMQ header".into());
        }
        if raw.get(0..4) != Some(&MAGIC) || raw[4] != VERSION {
            return Err("invalid ITMQ frame".into());
        }
        let priority = raw[5];
        let flags = u16_at(raw, 6)?;
        let event_time_ns = u64_at(raw, 8)?;
        let receive_time_ns = u64_at(raw, 16)?;
        let process_time_ns = u64_at(raw, 24)?;
        let source_seq = u64_at(raw, 32)?;
        let sl = u16_at(raw, 40)? as usize;
        let srcl = u16_at(raw, 42)? as usize;
        let tl = u16_at(raw, 44)? as usize;
        let pl = u32_at(raw, 46)? as usize;
        let want = u32_at(raw, 50)?;
        let body_len = sl.checked_add(srcl).and_then(|n| n.checked_add(tl))
            .and_then(|n| n.checked_add(pl)).ok_or_else(|| "frame overflow".to_string())?;
        let need = HEADER_SIZE.checked_add(body_len).ok_or_else(|| "frame overflow".to_string())?;
        if raw.len() != need {
            return Err("truncated/oversized ITMQ frame".into());
        }
        let body = &raw[HEADER_SIZE..];
        if crc32(body) != want {
            return Err("CRC mismatch".into());
        }

        let symbol_end = sl;
        let source_end = symbol_end + srcl;
        let type_end = source_end + tl;
        let payload_end = type_end + pl;
        let symbol = String::from_utf8(body[0..symbol_end].to_vec()).map_err(|_| "symbol utf8".to_string())?;
        let source = String::from_utf8(body[symbol_end..source_end].to_vec()).map_err(|_| "source utf8".to_string())?;
        let event_type = String::from_utf8(body[source_end..type_end].to_vec()).map_err(|_| "type utf8".to_string())?;
        let payload = body[type_end..payload_end].to_vec();
        Ok(Self { event_time_ns, receive_time_ns, process_time_ns, source_seq, priority, flags, symbol, source, event_type, payload })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wire_round_trip_and_crc() {
        let ev = EventEnvelope {
            event_time_ns: 100,
            receive_time_ns: 110,
            process_time_ns: 120,
            source_seq: 7,
            priority: PRIORITY_OPTION_TRADE,
            flags: 2,
            symbol: "DIA".into(),
            source: "OPRA".into(),
            event_type: "OPTION_TRADE".into(),
            payload: vec![0x81, 0xa1, b'p', 0x01],
        };
        let wire = ev.to_wire().unwrap();
        assert_eq!(wire.len(), HEADER_SIZE + 3 + 4 + 12 + 4);
        assert_eq!(EventEnvelope::from_wire(&wire).unwrap(), ev);
        let mut corrupt = wire.clone();
        let n = corrupt.len();
        corrupt[n - 1] ^= 0x01;
        assert!(EventEnvelope::from_wire(&corrupt).is_err());
    }
}
