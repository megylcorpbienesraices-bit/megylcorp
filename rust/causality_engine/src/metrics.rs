use std::{io::{Read,Write}, net::TcpListener, sync::{Arc,atomic::{AtomicBool,AtomicU64,Ordering}}, thread, time::Duration};

#[derive(Default)]
pub struct Metrics {
    pub received: AtomicU64,
    pub ordered: AtomicU64,
    pub published: AtomicU64,
    pub publish_errors: AtomicU64,
    pub heap_high_water: AtomicU64,
    pub late_dropped: AtomicU64,
    pub ingress_backpressure: AtomicU64,
    pub ingress_decode_errors: AtomicU64,
    pub outbound_queue_high_water: AtomicU64,
    pub heap_overflow_dropped: AtomicU64,
    pub outbound_backpressure: AtomicU64,
    pub outbound_timeout_dropped: AtomicU64,
}
impl Metrics {
    pub fn render(&self)->String{
        format!(
"# HELP itmq_rust_events_received_total Events accepted by the Rust orderer.\n# TYPE itmq_rust_events_received_total counter\nitmq_rust_events_received_total {}\n# HELP itmq_rust_events_ordered_total Events emitted causally.\n# TYPE itmq_rust_events_ordered_total counter\nitmq_rust_events_ordered_total {}\n# HELP itmq_rust_events_published_total Events published to ZeroMQ.\n# TYPE itmq_rust_events_published_total counter\nitmq_rust_events_published_total {}\n# HELP itmq_rust_publish_errors_total Nonblocking ZeroMQ publish failures.\n# TYPE itmq_rust_publish_errors_total counter\nitmq_rust_publish_errors_total {}\n# HELP itmq_rust_heap_high_water Maximum causal heap depth observed.\n# TYPE itmq_rust_heap_high_water gauge\nitmq_rust_heap_high_water {}\n# HELP itmq_rust_late_dropped_total Events arriving behind the already-emitted causal frontier.\n# TYPE itmq_rust_late_dropped_total counter\nitmq_rust_late_dropped_total {}\n# HELP itmq_rust_ingress_backpressure_total Ingress sends that observed a full bounded channel.\n# TYPE itmq_rust_ingress_backpressure_total counter\nitmq_rust_ingress_backpressure_total {}\n# HELP itmq_rust_ingress_decode_errors_total Invalid ITMQ binary frames rejected at ingress.\n# TYPE itmq_rust_ingress_decode_errors_total counter\nitmq_rust_ingress_decode_errors_total {}\n# HELP itmq_rust_outbound_queue_high_water Maximum pending events between orderer and publisher.\n# TYPE itmq_rust_outbound_queue_high_water gauge\nitmq_rust_outbound_queue_high_water {}\n# HELP itmq_rust_heap_overflow_dropped_total Events rejected because the causal heap reached its hard cap.\n# TYPE itmq_rust_heap_overflow_dropped_total counter\nitmq_rust_heap_overflow_dropped_total {}\n# HELP itmq_rust_outbound_backpressure_total Outbound sends that observed a full bounded channel.\n# TYPE itmq_rust_outbound_backpressure_total counter\nitmq_rust_outbound_backpressure_total {}\n# HELP itmq_rust_outbound_timeout_dropped_total Events dropped after bounded publisher backpressure timeout.\n# TYPE itmq_rust_outbound_timeout_dropped_total counter\nitmq_rust_outbound_timeout_dropped_total {}\n",
self.received.load(Ordering::Relaxed), self.ordered.load(Ordering::Relaxed), self.published.load(Ordering::Relaxed),
self.publish_errors.load(Ordering::Relaxed), self.heap_high_water.load(Ordering::Relaxed), self.late_dropped.load(Ordering::Relaxed),
self.ingress_backpressure.load(Ordering::Relaxed), self.ingress_decode_errors.load(Ordering::Relaxed), self.outbound_queue_high_water.load(Ordering::Relaxed),
self.heap_overflow_dropped.load(Ordering::Relaxed), self.outbound_backpressure.load(Ordering::Relaxed), self.outbound_timeout_dropped.load(Ordering::Relaxed))
    }
}

pub fn serve(metrics:Arc<Metrics>, running:Arc<AtomicBool>, addr:String){
    thread::spawn(move||{
        let listener=match TcpListener::bind(&addr){Ok(x)=>x,Err(e)=>{eprintln!("metrics bind {}: {}",addr,e);return;}};
        let _=listener.set_nonblocking(true);
        while running.load(Ordering::Relaxed){
            match listener.accept(){
                Ok((mut s,_))=>{let mut buf=[0u8;1024];let _=s.read(&mut buf);let body=metrics.render();let hdr=format!("HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",body.len());let _=s.write_all(hdr.as_bytes());let _=s.write_all(body.as_bytes());}
                Err(e) if e.kind()==std::io::ErrorKind::WouldBlock=>thread::sleep(Duration::from_millis(50)),
                Err(_)=>thread::sleep(Duration::from_millis(100)),
            }
        }
    });
}
