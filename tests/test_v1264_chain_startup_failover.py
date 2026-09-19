from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding='utf-8')


def test_alpaca_transient_gateway_retries_but_auth_does_not(monkeypatch):
    from app.core import alpaca_data as a
    class Resp:
        def __init__(self,status,payload): self.status_code=status; self._payload=payload; self.text=str(payload)
        def json(self): return self._payload
    calls=[]
    seq=[Resp(504,{'message':'backend request timeout'}),Resp(200,{'ok':True})]
    monkeypatch.setenv('ITM_QUANT_ALPACA_RETRY_ATTEMPTS','3')
    monkeypatch.setattr(a,'sleep',lambda *_:None)
    monkeypatch.setattr(a.requests,'get',lambda *args,**kwargs:(calls.append(1) or seq.pop(0)))
    out=a._get_json('https://example.test',a.AlpacaSettings('k','s'))
    assert out=={'ok':True} and len(calls)==2

    calls.clear()
    monkeypatch.setattr(a.requests,'get',lambda *args,**kwargs:(calls.append(1) or Resp(401,{'message':'unauthorized'})))
    try:
        a._get_json('https://example.test',a.AlpacaSettings('k','s'))
    except RuntimeError as exc:
        assert 'HTTP 401' in str(exc)
    else:
        raise AssertionError('401 must fail')
    assert len(calls)==1


def test_transient_chain_classifier_is_narrow():
    from app.service import is_transient_chain_failure
    assert is_transient_chain_failure(RuntimeError('Alpaca HTTP 504: backend request timeout'))
    assert is_transient_chain_failure(RuntimeError('NO_OWN_STRUCTURAL_CONTRACTS_IN_MEMORY'))
    assert not is_transient_chain_failure(RuntimeError('Alpaca HTTP 401: unauthorized'))


def test_tasty_failover_wait_uses_only_real_in_memory_chain(monkeypatch):
    import app.service as svc
    frame=pd.DataFrame({'strike':[530.0],'option_type':['call'],'expiration_date':['2026-09-11'],'underlying_price':[530.0]})
    meta={'ready':True,'source':'TASTYTRADE_DXLINK LIVE MEMORY'}
    state={'n':0}
    monkeypatch.setattr(type(svc.TASTYTRADE),'configured',property(lambda self: True))
    def fake(*args,**kwargs):
        state['n']+=1
        if state['n']<3: raise RuntimeError('NO_OWN_STRUCTURAL_CONTRACTS_IN_MEMORY')
        return frame,meta
    monkeypatch.setattr(svc,'_tastytrade_own_chain_snapshot',fake)
    monkeypatch.setattr(svc._time,'sleep',lambda *_:None)
    out,m=svc._wait_tastytrade_own_chain('DIA',12,21,wait_seconds=0.1)
    assert not out.empty
    assert m['structural_route']=='TASTYTRADE_DXLINK_WARM_FAILOVER'
    assert state['n']>=3


def test_boot_order_and_quant_retry_contract_are_enforced_in_source():
    main=text('app/main.py')
    provider=main.index('asyncio.create_task(TASTYTRADE.start(STATE.symbol)')
    quant=main.index('initial_quant = asyncio.create_task(_warm_asset_quant')
    assert provider < quant
    assert 'phase="CHAIN_RETRY"' in main
    assert 'is_transient_chain_failure(exc)' in main
    assert 'for attempt in range(3):' in main
    assert 'deadline=loop.time()+timeout' in main


def test_asset_switch_prepares_tasty_before_quant_warmup():
    main=text('app/main.py')
    block=main[main.index('@app.post("/api/asset/select")'):main.index('@app.get("/api/replay/sessions")')]
    # Lo que importa es el ORDEN: DXLink arranca antes que la hidratación pesada de
    # Quant. Antes esto se comprobaba contra el texto exacto de la llamada, así que
    # el test se rompía al refactorizar aunque el orden siguiera siendo correcto.
    assert block.index('_schedule_tastytrade_prepare(target, epoch)') < block.index('_schedule_asset_warmup(target, epoch)')
    assert 'if provider!="TASTYTRADE" and TASTYTRADE.configured:' in block


def test_background_prepare_task_keeps_a_strong_reference():
    """El bucle de eventos solo guarda referencias DÉBILES a las tareas.

    Cuando la única referencia fuerte era una variable local del handler, la tarea
    quedaba huérfana en cuanto el handler retornaba y el recolector podía matarla a
    media ejecución. Justo la tarea que adelanta DXLink para que el SIGUIENTE activo
    cargue rápido: al perderse, el cambio de activo se quedaba sin failover preparado
    y nadie veía un error, solo iba lento.
    """
    main=text('app/main.py')
    assert '_TASTY_PREPARE_TASKS' in main
    block=main[main.index('def _schedule_tastytrade_prepare'):main.index('def _schedule_trace_bootstrap')]
    assert '_TASTY_PREPARE_TASKS[int(epoch)]=task' in block, 'la tarea debe quedar registrada'
    assert 'add_done_callback' in block, 'debe liberarse y reportar su excepción al terminar'
    # Ningún create_task del handler de cambio de activo puede quedar sin registrar.
    switch=main[main.index('@app.post("/api/asset/select")'):main.index('@app.get("/api/replay/sessions")')]
    assert 'asyncio.create_task(' not in switch, 'las tareas de fondo se lanzan vía planificador registrado'


def test_release_keeps_dow_truth_and_persistence_contracts():
    service=text('app/service.py')
    readme=text('README.md')
    persistence=text('app/persistence.py')
    assert 'Scanner' in readme and 'única autoridad direccional' in readme
    assert 'DIA' in service and 'YM' in service and 'DJX' in service
    assert 'replay' in persistence.lower() or 'history' in persistence.lower()
