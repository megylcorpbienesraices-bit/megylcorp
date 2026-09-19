"""Measured transport policy: JSON control, NDJSON history, binary/MessagePack LIVE."""

from __future__ import annotations

from typing import Any
import json
import statistics
import time
import msgpack

from .binary_protocol import BinaryEvent


def _sample() -> dict[str,Any]:
    return {"symbol":"DIA","source":"TASTYTRADE","event_type":"GREEKS","timestamp":"2026-09-10T14:30:00Z","values":{"bid":532.14,"ask":532.15,"delta":0.54,"gamma":0.021,"iv":0.19,"oi":18420,"volume":5210},"tags":["LIVE","DERIVATIVES","QUALITY_OK"]}



def transport_policy() -> dict[str, str]:
    """Cheap transport contract for health/coverage endpoints; no benchmark on hot path."""
    return {
        "control": "JSON",
        "large_history": "NDJSON_STREAM",
        "ticks": "ITMT_FIXED_BINARY",
        "surface": "ITMS_FLOAT32_BINARY",
        "generic_live": "MSGPACK_OR_ITMQ_BINARY_SELECTED_BY_BENCHMARK",
    }

def benchmark_transport(iterations:int=1500)->dict[str,Any]:
    n=max(100,min(int(iterations),10000));payload=_sample();results=[]
    def bench(name,enc,dec):
        enc_times=[];dec_times=[];size=0;blob=None
        for _ in range(n):
            t=time.perf_counter_ns();blob=enc(payload);enc_times.append(time.perf_counter_ns()-t);size=len(blob)
        for _ in range(n):
            t=time.perf_counter_ns();dec(blob);dec_times.append(time.perf_counter_ns()-t)
        results.append({"codec":name,"bytes":size,"encode_us_median":round(statistics.median(enc_times)/1000,3),"decode_us_median":round(statistics.median(dec_times)/1000,3)})
    bench("JSON",lambda p:json.dumps(p,separators=(",",":")).encode(),lambda b:json.loads(b))
    bench("MSGPACK",lambda p:msgpack.packb(p,use_bin_type=True),lambda b:msgpack.unpackb(b,raw=False))
    def enc_itmq(p):
        return BinaryEvent(1,2,3,4,1,p["symbol"],p["source"],p["event_type"],p["values"]).pack()
    bench("ITMQ_BINARY_MSGPACK",enc_itmq,lambda b:BinaryEvent.unpack(b))
    generic=min([r for r in results if r["codec"] in {"MSGPACK","ITMQ_BINARY_MSGPACK"}],key=lambda r:(r["decode_us_median"]+r["encode_us_median"],r["bytes"]))
    return {"ready":True,"iterations":n,"results":results,"selected_generic_live":generic["codec"],
            "policy":{**transport_policy(),"generic_live":generic["codec"]},
            "note":"Codec selection is benchmark-driven. Protobuf is not added merely by name; current MessagePack/explicit binary contract is retained unless an external benchmark beats it."}
