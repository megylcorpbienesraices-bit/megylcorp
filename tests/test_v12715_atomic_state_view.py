from __future__ import annotations

import threading
import time
import pytest

from app.service import PlatformState, StateView, VIEW_FIELDS

CAMPOS=("gamma_delta","scanner","calibration","regime_context","market_state_field","feature_intelligence_report")

def _publish(state: PlatformState, gen: int, sym: str, pause: bool=True):
    with state.lock:
        state.set_active(sym, gen)
        for i,name in enumerate(CAMPOS):
            setattr(state,name,{"gen":gen,"sym":sym})
            if pause and i in (1,3):
                time.sleep(0)
        state.publish_view("FULL")

def _read_view(state: PlatformState)->bool:
    v=state.view()
    if v.generation <= 1 or not all(isinstance(v.get(c),dict) and v.get(c) for c in CAMPOS):
        return True
    gens={v.get(c).get("gen") for c in CAMPOS}
    syms={v.get(c).get("sym") for c in CAMPOS}|{v.symbol}
    return len(gens)==1 and len(syms)==1 and v.symbol_epoch in gens

def test_vista_publicada_siempre_coherente():
    st=PlatformState()
    stop=threading.Event(); bad=[0]; reads=[0]
    def reader():
        while not stop.is_set():
            reads[0]+=1
            if not _read_view(st): bad[0]+=1
            if reads[0] % 64 == 0:
                time.sleep(0)
    th=[threading.Thread(target=reader,daemon=True) for _ in range(4)]
    for t in th:t.start()
    for g in range(1,160):
        _publish(st,g,"DIA" if g%2 else "SPY")
        time.sleep(0.0001)
    stop.set()
    for t in th:t.join(2)
    assert reads[0]>500
    assert bad[0]==0

def test_lectura_directa_puede_romperse():
    # Deterministic demonstration of the old failure mode: fields are deliberately
    # changed one-by-one before the atomic view is published.
    st=PlatformState(); st.set_active("DIA",1)
    for c in CAMPOS:setattr(st,c,{"gen":1,"sym":"DIA"})
    st.publish_view("FULL")
    with st.lock:
        st.set_active("SPY",2)
        st.gamma_delta={"gen":2,"sym":"SPY"}
        direct={getattr(st,c).get("gen") for c in CAMPOS}
        view={st.view().get(c).get("gen") for c in CAMPOS}
    assert len(direct)>1, "the old direct-read pattern should be demonstrably cross-generation"
    assert view=={1}

def test_par_simbolo_epoch_nunca_se_desempareja():
    st=PlatformState(); expected={0:st.symbol}; stop=threading.Event(); bad=[0]; n=[0]
    def reader():
        while not stop.is_set():
            sym,epoch=st.active(); n[0]+=1
            if epoch in expected and expected[epoch]!=sym: bad[0]+=1
    th=[threading.Thread(target=reader,daemon=True) for _ in range(4)]
    for t in th:t.start()
    for g in range(1,250):
        sym="DIA" if g%2 else "SPY"; expected[g]=sym; st.set_active(sym,g)
    stop.set()
    for t in th:t.join(2)
    assert n[0]>100 and bad[0]==0

def test_vista_es_inmutable_y_no_cambia_tras_publicarse():
    st=PlatformState(); st.scanner={"gen":1}; v1=st.publish_view("FULL")
    st.scanner={"gen":2}; v2=st.publish_view("FULL")
    assert v1.get("scanner")=={"gen":1} and v2.get("scanner")=={"gen":2}
    with pytest.raises(Exception):
        v1.generation=99

def test_vista_cubre_campos_criticos_de_endpoints():
    critical={"symbol","symbol_epoch","scanner","gamma_delta","calibration","regime_context","market_state_field",
              "feature_intelligence_report","research_storage_report","derivatives_intelligence_report",
              "structural_intelligence_report","history","snapshot","mode","data_quality_report","expiry_window","replay_context"}
    assert not (critical-set(VIEW_FIELDS))

def test_stage_distingue_core_de_full():
    st=PlatformState(); core=st.publish_view("CORE"); full=st.publish_view("FULL")
    assert isinstance(core,StateView) and isinstance(full,StateView)
    assert core.stage=="CORE" and full.stage=="FULL" and full.generation==core.generation+1
