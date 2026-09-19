use std::{env, process::Command, time::{SystemTime, UNIX_EPOCH}};

#[derive(Debug, Clone)]
pub struct ClockStatus {
    pub source: &'static str,
    pub ptp_active: bool,
    pub offset_ns: Option<i64>,
    pub quality: &'static str,
    pub checked_ns: u64,
}

pub fn probe() -> ClockStatus {
    let configured = env::var("ITM_PTP_ENABLED").map(|v| matches!(v.to_ascii_lowercase().as_str(), "1"|"true"|"yes"|"on")).unwrap_or(false)
        || env::var("ITM_PTP_INTERFACE").map(|v| !v.trim().is_empty()).unwrap_or(false);
    let checked_ns = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos() as u64;
    if configured {
        if let Ok(out) = Command::new("pmc").args(["-u","-b","0","GET TIME_STATUS_NP"]).output() {
            if out.status.success() {
                let text = String::from_utf8_lossy(&out.stdout);
                for line in text.lines() {
                    if let Some(rest) = line.trim().strip_prefix("master_offset") {
                        if let Ok(offset) = rest.trim().parse::<i64>() {
                            return ClockStatus { source:"PTP", ptp_active:true, offset_ns:Some(offset), quality:if offset.abs()<=500{"HIGH"}else{"DEGRADED"}, checked_ns };
                        }
                    }
                }
            }
        }
    }
    ClockStatus { source:"SYSTEM/NTP", ptp_active:false, offset_ns:None, quality:"MEDIUM", checked_ns }
}
