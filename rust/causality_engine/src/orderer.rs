use crate::protocol::EventEnvelope;
use crate::metrics::Metrics;
use crossbeam_channel::{Receiver, Sender, TrySendError, SendTimeoutError};
use std::{cmp::Ordering, collections::BinaryHeap, sync::{Arc, atomic::{AtomicBool, Ordering as AtomicOrdering}}, time::Duration};

#[derive(Debug, Clone)]
struct HeapItem(EventEnvelope);
impl Eq for HeapItem {}
impl PartialEq for HeapItem { fn eq(&self, other:&Self)->bool { self.0.ordering_key()==other.0.ordering_key() } }
impl Ord for HeapItem {
    fn cmp(&self, other:&Self)->Ordering {
        // Reverse comparisons: std::BinaryHeap becomes a deterministic min-heap.
        other.0.event_time_ns.cmp(&self.0.event_time_ns)
            .then_with(|| other.0.source_seq.cmp(&self.0.source_seq))
            .then_with(|| other.0.priority.cmp(&self.0.priority))
            .then_with(|| other.0.source.cmp(&self.0.source))
    }
}
impl PartialOrd for HeapItem { fn partial_cmp(&self, other:&Self)->Option<Ordering>{Some(self.cmp(other))} }

pub struct OrdererConfig { pub max_lateness_ns:u64, pub heap_capacity:usize, pub idle_park_ns:u64 }
impl Default for OrdererConfig {
    fn default()->Self { Self{max_lateness_ns:350_000_000,heap_capacity:200_000,idle_park_ns:50_000} }
}

fn update_high_water(dst:&std::sync::atomic::AtomicU64, value:u64){
    let mut old=dst.load(AtomicOrdering::Relaxed);
    while value>old {match dst.compare_exchange_weak(old,value,AtomicOrdering::Relaxed,AtomicOrdering::Relaxed){Ok(_)=>break,Err(v)=>old=v}}
}

fn send_bounded(tx:&Sender<EventEnvelope>, ev:EventEnvelope, metrics:&Metrics)->bool{
    match tx.try_send(ev){
        Ok(_)=>true,
        Err(TrySendError::Full(ev))=>{
            metrics.outbound_backpressure.fetch_add(1,AtomicOrdering::Relaxed);
            // Bounded wait: propagate pressure without allowing shutdown to deadlock
            // forever if the publisher has stopped draining the channel.
            match tx.send_timeout(ev,Duration::from_millis(100)){
                Ok(_)=>true,
                Err(SendTimeoutError::Timeout(_))=>{
                    metrics.outbound_timeout_dropped.fetch_add(1,AtomicOrdering::Relaxed);
                    true
                },
                Err(SendTimeoutError::Disconnected(_))=>false,
            }
        },
        Err(TrySendError::Disconnected(_))=>false,
    }
}

pub fn run_orderer(rx:Receiver<EventEnvelope>, tx:Sender<EventEnvelope>, cfg:OrdererConfig, running:Arc<AtomicBool>, metrics:Arc<Metrics>) {
    let mut heap=BinaryHeap::with_capacity(cfg.heap_capacity);
    let mut watermark=0u64;
    let mut last_emitted:Option<(u64,u64,u8,String)>=None;
    while running.load(AtomicOrdering::Relaxed) {
        let mut drained=0usize;
        while let Ok(ev)=rx.try_recv() {
            metrics.received.fetch_add(1,AtomicOrdering::Relaxed);
            let key=(ev.event_time_ns,ev.source_seq,ev.priority,ev.source.clone());
            // Once an event has crossed the output frontier, injecting an older key would
            // make the public stream non-monotonic.  Such packets are quarantined by metric
            // rather than silently corrupting Replay/Dealer classification.
            if last_emitted.as_ref().map(|k| key < *k).unwrap_or(false) {
                metrics.late_dropped.fetch_add(1,AtomicOrdering::Relaxed);
                continue;
            }
            watermark=watermark.max(ev.event_time_ns);
            if heap.len()>=cfg.heap_capacity.max(1){
                metrics.heap_overflow_dropped.fetch_add(1,AtomicOrdering::Relaxed);
                continue;
            }
            heap.push(HeapItem(ev));drained+=1;
            update_high_water(&metrics.heap_high_water,heap.len() as u64);
        }
        let cutoff=watermark.saturating_sub(cfg.max_lateness_ns);
        while let Some(top)=heap.peek() {
            if top.0.event_time_ns>cutoff {break}
            if let Some(item)=heap.pop() {
                last_emitted=Some((item.0.event_time_ns,item.0.source_seq,item.0.priority,item.0.source.clone()));
                metrics.ordered.fetch_add(1,AtomicOrdering::Relaxed);
                if !send_bounded(&tx,item.0,&metrics){return;}
                update_high_water(&metrics.outbound_queue_high_water,tx.len() as u64);
            }
        }
        if drained==0 { std::thread::park_timeout(Duration::from_nanos(cfg.idle_park_ns.max(1_000))); }
    }
    // Graceful shutdown flush: pop directly from the reversed BinaryHeap; each pop is
    // the oldest remaining causal key.  No ambiguous into_sorted_vec reversal.
    while let Some(item)=heap.pop(){
        metrics.ordered.fetch_add(1,AtomicOrdering::Relaxed);
        if !send_bounded(&tx,item.0,&metrics){break;}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::EventEnvelope;
    fn ev(t:u64,seq:u64)->EventEnvelope{EventEnvelope{event_time_ns:t,receive_time_ns:t,process_time_ns:t,source_seq:seq,priority:20,flags:0,symbol:"DIA".into(),source:"TEST".into(),event_type:"TRADE".into(),payload:Vec::new()}}
    #[test] fn min_heap_order(){let mut h=BinaryHeap::new();h.push(HeapItem(ev(20,1)));h.push(HeapItem(ev(10,2)));h.push(HeapItem(ev(10,1)));assert_eq!(h.pop().unwrap().0.source_seq,1);assert_eq!(h.pop().unwrap().0.source_seq,2);}
}
