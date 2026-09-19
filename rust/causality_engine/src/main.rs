// Engine lineage retained; release identity follows Cargo package metadata.
mod protocol; mod orderer; mod metrics; mod provider; mod ring; mod clock; mod sentinel; mod source_arbitration; mod simd;
use protocol::EventEnvelope;
use orderer::{run_orderer,OrdererConfig};
use metrics::Metrics;
use provider::{DirectFeedAdapter,WireReplayAdapter};
use crossbeam_channel::{bounded,TrySendError};
use std::{env, io::Write, sync::{Arc, atomic::{AtomicBool, Ordering}}, thread};

fn pin_current(index:usize){if let Some(ids)=core_affinity::get_core_ids(){if let Some(id)=ids.get(index%ids.len()){core_affinity::set_for_current(*id);}}}

fn main(){
    let endpoint=env::var("ITM_RUST_ZMQ_BIND").unwrap_or_else(|_|"tcp://127.0.0.1:5555".into());
    let ingress_capacity=env::var("ITM_RUST_INGRESS_CAPACITY").ok().and_then(|v|v.parse().ok()).unwrap_or(1_000_000usize);
    let outbound_capacity=env::var("ITM_RUST_OUTBOUND_CAPACITY").ok().and_then(|v|v.parse().ok()).unwrap_or(250_000usize);
    let heap_capacity=env::var("ITM_RUST_HEAP_CAPACITY").ok().and_then(|v|v.parse().ok()).unwrap_or(200_000usize);
    let lateness_ms=env::var("ITM_CAUSAL_MAX_LATENESS_MS").ok().and_then(|v|v.parse::<f64>().ok()).unwrap_or(350.0);
    let replay_path=env::var("ITM_RUST_REPLAY_WIRE").ok().filter(|s|!s.trim().is_empty());
    let ingest_bind=env::var("ITM_RUST_INGEST_BIND").ok().filter(|s|!s.trim().is_empty());
    let spool_path=env::var("ITM_RUST_SPOOL_WIRE").ok().filter(|s|!s.trim().is_empty());
    let metrics_addr=env::var("ITM_RUST_METRICS_ADDR").unwrap_or_else(|_|"127.0.0.1:9555".into());
    let (tx_in,rx_in)=bounded::<EventEnvelope>(ingress_capacity);
    // v1.27.13: every queue is physically bounded. A slow publisher must create
    // backpressure, not unbounded RAM growth.
    let (tx_out,rx_out)=bounded::<EventEnvelope>(outbound_capacity.max(1));
    let running=Arc::new(AtomicBool::new(true));let r=running.clone();ctrlc::set_handler(move||{r.store(false,Ordering::SeqCst);}).expect("ctrl-c handler");
    let metrics=Arc::new(Metrics::default());metrics::serve(metrics.clone(),running.clone(),metrics_addr.clone());
    let clk=clock::probe(); eprintln!("CLOCK source={} ptp={} offset_ns={:?} quality={}",clk.source,clk.ptp_active,clk.offset_ns,clk.quality);
    eprintln!("SIMD {:?} · AVX512 is promoted only after parity validation on target CPU",simd::detected_backend());

    let run_ing=running.clone();let m1=metrics.clone();let ing=thread::spawn(move||{pin_current(0);
        if let Some(path)=replay_path {match WireReplayAdapter::open(&path){Ok(mut a)=>{eprintln!("INGEST {} {}",a.name(),path);while run_ing.load(Ordering::Relaxed){match a.next_event(){Ok(Some(ev))=>match tx_in.try_send(ev){Ok(_)=>{},Err(TrySendError::Full(ev))=>{m1.ingress_backpressure.fetch_add(1,Ordering::Relaxed);if tx_in.send(ev).is_err(){break}},Err(TrySendError::Disconnected(_))=>break},Ok(None)=>break,Err(e)=>{eprintln!("replay ingest: {}",e);break}}}},Err(e)=>eprintln!("replay open: {}",e)}}
        else if let Some(bind)=ingest_bind {
            let ctx=zmq::Context::new();let pull=ctx.socket(zmq::PULL).expect("PULL");
            pull.set_rcvhwm(ingress_capacity as i32).ok();pull.set_linger(0).ok();
            pull.bind(&bind).expect("ZeroMQ ingress bind");eprintln!("INGEST ITMQ-WIRE PULL {}",bind);
            while run_ing.load(Ordering::Relaxed){
                match pull.recv_bytes(zmq::DONTWAIT){
                    Ok(raw)=>match EventEnvelope::from_wire(&raw){
                        Ok(ev)=>match tx_in.try_send(ev){Ok(_)=>{},Err(TrySendError::Full(ev))=>{m1.ingress_backpressure.fetch_add(1,Ordering::Relaxed);if tx_in.send(ev).is_err(){break}},Err(TrySendError::Disconnected(_))=>break},
                        Err(_)=>{m1.ingress_decode_errors.fetch_add(1,Ordering::Relaxed);}
                    },
                    Err(e) if e==zmq::Error::EAGAIN=>thread::park_timeout(std::time::Duration::from_micros(50)),
                    Err(e)=>{eprintln!("ingress recv: {}",e);thread::park_timeout(std::time::Duration::from_millis(1));}
                }
            }
        } else {eprintln!("INGEST READY · no direct provider/replay/Python ingress configured; no synthetic market events will be generated");while run_ing.load(Ordering::Relaxed){thread::park_timeout(std::time::Duration::from_millis(50));}}
        drop(tx_in);
    });

    let run_ord=running.clone();let m2=metrics.clone();let ord=thread::spawn(move||{pin_current(1);run_orderer(rx_in,tx_out,OrdererConfig{max_lateness_ns:(lateness_ms*1_000_000.0) as u64,heap_capacity:heap_capacity.max(1),..Default::default()},run_ord,m2);});
    let run_pub=running.clone();let m3=metrics.clone();let pubth=thread::spawn(move||{pin_current(2);let ctx=zmq::Context::new();let pubsock=ctx.socket(zmq::PUB).expect("PUB");pubsock.set_sndhwm(250_000).ok();pubsock.set_linger(0).ok();pubsock.bind(&endpoint).expect("ZeroMQ bind");let mut spool=spool_path.and_then(|p|std::fs::OpenOptions::new().create(true).append(true).open(p).ok());
        eprintln!("ITM QUANT Rust Causality Engine v{} PUB {} · metrics {}", env!("CARGO_PKG_VERSION"), endpoint, metrics_addr);
        while run_pub.load(Ordering::Relaxed){match rx_out.recv_timeout(std::time::Duration::from_millis(50)){Ok(ev)=>if let Ok(bytes)=ev.to_wire(){if let Some(f)=spool.as_mut(){let n=(bytes.len() as u32).to_le_bytes();let _=f.write_all(&n);let _=f.write_all(&bytes);}match pubsock.send(bytes,zmq::DONTWAIT){Ok(_)=>{m3.published.fetch_add(1,Ordering::Relaxed);},Err(_)=>{m3.publish_errors.fetch_add(1,Ordering::Relaxed);}}},Err(crossbeam_channel::RecvTimeoutError::Disconnected)=>break,Err(_)=>{}}}
    });
    let _=ing.join();let _=ord.join();let _=pubth.join();
}
