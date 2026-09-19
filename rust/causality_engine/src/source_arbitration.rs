#[derive(Debug, Clone)]
pub struct SourceHealth {
    pub name: String,
    pub latency_ms: f64,
    pub age_ms: f64,
    pub gap_rate: f64,
    pub sequence_ok: bool,
    pub divergence: f64,
    pub live: bool,
}
#[derive(Debug, Clone)]
pub struct RankedSource { pub name:String, pub score:f64 }

pub fn score(s:&SourceHealth)->f64{
    let latency=(1.0-(s.latency_ms.max(0.0)/500.0).min(1.0)).max(0.0);
    let fresh=(1.0-(s.age_ms.max(0.0)/5000.0).min(1.0)).max(0.0);
    let gaps=(1.0-s.gap_rate.clamp(0.0,1.0)).max(0.0);
    let div=(1.0-(s.divergence.abs()/0.01).min(1.0)).max(0.0);
    // Rust exige parte entera en los literales flotantes: `.27` no compila, a
    // diferencia de Python. Los pesos son los mismos; sólo se escriben completos.
    100.0*(0.27*latency+0.27*fresh+0.18*gaps+0.13*if s.sequence_ok{1.0}else{0.0}+0.10*div+0.05*if s.live{1.0}else{0.0})
}
pub fn rank(sources:&[SourceHealth])->Vec<RankedSource>{let mut x:Vec<_>=sources.iter().map(|s|RankedSource{name:s.name.clone(),score:score(s)}).collect();x.sort_by(|a,b|b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));x}
