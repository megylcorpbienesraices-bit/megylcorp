//! Provider boundary for direct/PCAP feeds.
//! A provider adapter must decode its native protocol into EventEnvelope.  This crate
//! deliberately does not pretend OPRA/Databento/CME wire formats are interchangeable.
use crate::protocol::EventEnvelope;

pub trait DirectFeedAdapter: Send {
    fn name(&self)->&'static str;
    fn next_event(&mut self)->Result<Option<EventEnvelope>,String>;
}

/// Integration-test/replay adapter for the stable ITMQ wire spool format.  The file is
/// a sequence of [u32 little-endian length][ITMQ event frame].  Production direct-feed
/// decoders can be added without changing the causal orderer/publisher.
pub struct WireReplayAdapter { file: std::fs::File }
impl WireReplayAdapter { pub fn open(path:&str)->Result<Self,String>{std::fs::File::open(path).map(|file|Self{file}).map_err(|e|e.to_string())} }
impl DirectFeedAdapter for WireReplayAdapter {
    fn name(&self)->&'static str{"ITMQ_WIRE_REPLAY"}
    fn next_event(&mut self)->Result<Option<EventEnvelope>,String>{
        use std::io::Read;
        let mut l=[0u8;4];
        match self.file.read_exact(&mut l){Ok(_)=>{},Err(e) if e.kind()==std::io::ErrorKind::UnexpectedEof=>return Ok(None),Err(e)=>return Err(e.to_string())}
        let n=u32::from_le_bytes(l) as usize; if n<crate::protocol::HEADER_SIZE||n>64*1024*1024{return Err("invalid replay frame length".into())}
        let mut b=vec![0u8;n];self.file.read_exact(&mut b).map_err(|e|e.to_string())?;
        crate::protocol::EventEnvelope::from_wire(&b).map(Some)
    }
}

/// Provider-neutral adapter catalog for v1.24.  These descriptors are readiness
/// metadata only; they never claim a proprietary wire decoder is active merely
/// because the boundary exists.
#[derive(Debug,Clone,Copy)]
pub enum FeedKind { AlpacaWebSocket, Databento, OpraDirect, CmeDirect, UdpDirect, ItmqReplay }

impl FeedKind {
    pub fn name(&self)->&'static str { match self {
        FeedKind::AlpacaWebSocket=>"AlpacaWebSocketAdapter",
        FeedKind::Databento=>"DatabentoAdapter",
        FeedKind::OpraDirect=>"OPRAFeedAdapter",
        FeedKind::CmeDirect=>"CMEAdapter",
        FeedKind::UdpDirect=>"UDPDirectAdapter",
        FeedKind::ItmqReplay=>"ReplayAdapter",
    }}
    pub fn transport(&self)->&'static str { match self {
        FeedKind::AlpacaWebSocket=>"WEBSOCKET/HTTPS",
        FeedKind::Databento=>"BINARY/STREAM",
        FeedKind::OpraDirect=>"MULTICAST/PCAP",
        FeedKind::CmeDirect=>"MDP/MULTICAST",
        FeedKind::UdpDirect=>"UDP/NONBLOCKING",
        FeedKind::ItmqReplay=>"ITMQ_WIRE/FILE",
    }}
}

/// A native provider implementation must decode its licensed/native packet into
/// EventEnvelope before entering the causal orderer.  Raw UDP is deliberately not
/// assumed to mean OPRA/CME.
pub struct AdapterReadiness {
    pub kind: FeedKind,
    pub configured: bool,
    pub active: bool,
    pub detail: &'static str,
}

pub fn adapter_catalog()->Vec<AdapterReadiness>{vec![
    AdapterReadiness{kind:FeedKind::AlpacaWebSocket,configured:false,active:false,detail:"Python/API bridge; not UDP multicast."},
    AdapterReadiness{kind:FeedKind::Databento,configured:false,active:false,detail:"Requires provider entitlement/SDK or native decoder."},
    AdapterReadiness{kind:FeedKind::OpraDirect,configured:false,active:false,detail:"Requires licensed OPRA multicast/PCAP decoder."},
    AdapterReadiness{kind:FeedKind::CmeDirect,configured:false,active:false,detail:"Requires licensed CME MDP endpoint/decoder."},
    AdapterReadiness{kind:FeedKind::UdpDirect,configured:false,active:false,detail:"Generic raw gateway; decoder must be explicitly selected."},
    AdapterReadiness{kind:FeedKind::ItmqReplay,configured:false,active:false,detail:"Stable ITMQ replay wire format."},
]}
