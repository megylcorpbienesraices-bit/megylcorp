#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SentinelState { Green, Caution, Halt, Lock }

#[derive(Debug, Clone, Copy)]
pub struct SentinelLimits {
    pub max_feed_age_ms: f64,
    pub max_latency_ms: f64,
    pub max_clock_offset_ns: i64,
}
impl Default for SentinelLimits { fn default()->Self { Self{max_feed_age_ms:2500.0,max_latency_ms:750.0,max_clock_offset_ns:2_000_000} } }

#[derive(Debug, Clone)]
pub struct SentinelDecision {
    pub state: SentinelState,
    pub halt_new_orders: bool,
    pub flatten_requires_execution_adapter: bool,
    pub reasons: Vec<&'static str>,
}

pub fn evaluate(feed_age_ms: Option<f64>, latency_ms: Option<f64>, clock_offset_ns: Option<i64>, limits: SentinelLimits) -> SentinelDecision {
    let mut reasons=Vec::new(); let mut severe=0usize;
    if feed_age_ms.map(|x|x>limits.max_feed_age_ms).unwrap_or(false){reasons.push("STALE_FEED");severe+=1;}
    if latency_ms.map(|x|x>limits.max_latency_ms).unwrap_or(false){reasons.push("LATENCY");}
    if clock_offset_ns.map(|x|x.abs()>limits.max_clock_offset_ns).unwrap_or(false){reasons.push("CLOCK_DRIFT");severe+=1;}
    let state=if severe>=2{SentinelState::Lock}else if severe==1{SentinelState::Halt}else if !reasons.is_empty(){SentinelState::Caution}else{SentinelState::Green};
    SentinelDecision{state,halt_new_orders:matches!(state,SentinelState::Halt|SentinelState::Lock),flatten_requires_execution_adapter:matches!(state,SentinelState::Lock),reasons}
}
