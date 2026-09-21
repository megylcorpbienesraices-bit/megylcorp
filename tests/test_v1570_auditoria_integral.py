"""AUDITORÍA INTEGRAL v1.57.0 · cuatro defectos que la suite en verde no veía.

Los cuatro son de la misma familia: nada estaba «roto» en el sentido de lanzar
una excepción o pintar un número imposible, así que 2.539 pruebas en verde los
tapaban perfectamente. Se ven midiendo, no leyendo.

    1. El último valor bueno no caducaba nunca.
    2. El acumulado de sesión tampoco.
    3. La hora del Scanner era local y sin zona, con el resto del proyecto en UTC.
    4. Describir un nivel avanzaba su contador de persistencia.
    5. Atribuir concentraciones a operaciones era cuadrático.

Cada prueba mide la MAGNITUD, no la intención: cuántas entradas quedan vivas,
cuántas restas de instantes se hacen, qué dice `tzinfo`. Una corrección que se
deshaga vuelve a fallar aquí aunque el código siga compilando.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# 1 · EL ÚLTIMO VALOR BUENO CADUCA
# ═══════════════════════════════════════════════════════════════════════════

def test_el_lkg_no_crece_sin_limite_al_pasar_las_sesiones():
    """30 sesiones x 8 activos x 10 carriles eran 2.400 entradas vivas.

    La clave lleva la fecha dentro, así que cada día abría entradas nuevas y
    ninguna se borraba jamás. En un portátil que se reinicia cada tarde no se
    nota; en el VPS, que no se apaga, el proceso engorda hasta que alguien lo
    mata.
    """
    from app.core import flow_view as FV
    FV.reset()
    activos = ["DIA", "SPY", "QQQ", "IWM", "TSLA", "NVDA", "AAPL", "MSFT"]
    carriles = [f"dataset_{i}" for i in range(10)]
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for d in range(30):
        dia = (base + timedelta(days=d)).date().isoformat()
        for sym in activos:
            for ds in carriles:
                FV.remember(sym, dia, ds, {"payload": [0] * 50})

    vivas = FV.coverage()["count"]
    fechas = {tuple(k)[1] for k in FV._LKG}
    assert len(fechas) <= FV.RETAIN_SESSIONS, (
        f"quedan {len(fechas)} fechas de sesión guardadas; el tope es "
        f"{FV.RETAIN_SESSIONS}")
    assert vivas == len(activos) * len(carriles) * FV.RETAIN_SESSIONS, (
        f"{vivas} entradas vivas tras 30 sesiones: el almacén sigue sin purgar")
    FV.reset()


def test_el_lkg_conserva_las_sesiones_recientes_y_tira_las_viejas():
    """Purgar de más sería peor que no purgar: se perdería el dato de ayer."""
    from app.core import flow_view as FV
    FV.reset()
    for d in range(1, 7):
        FV.remember("DIA", f"2026-03-0{d}", "tape", {"rows": d})
    assert FV.recall("DIA", "2026-03-06", "tape")["value"] == {"rows": 6}
    assert FV.recall("DIA", "2026-03-05", "tape")["value"] == {"rows": 5}
    assert FV.recall("DIA", "2026-03-04", "tape")["value"] == {"rows": 4}
    assert FV.recall("DIA", "2026-03-03", "tape") is None, "2026-03-03 debió caducar"
    cov = FV.coverage()
    assert cov["retain_sessions"] == FV.RETAIN_SESSIONS
    assert cov["session_dates"] == ["2026-03-06", "2026-03-05", "2026-03-04"]
    FV.reset()


def test_el_acumulado_de_sesion_tampoco_crece_sin_limite():
    """`close_session` sella pero no borra, y nadie borraba después."""
    from app.core import session_mode as SM
    SM.reset()
    activos = ["DIA", "SPY", "QQQ", "IWM", "TSLA", "NVDA", "AAPL", "MSFT"]
    # Mediodía de Nueva York en cada uno de 40 días hábiles seguidos.
    dia = datetime(2026, 1, 5, 17, 0, tzinfo=timezone.utc)
    contados = 0
    while contados < 40:
        if dia.weekday() < 5:
            for sym in activos:
                SM.accumulate(sym, price=100.0, volume=10.0, now=dia)
            contados += 1
        dia += timedelta(days=1)

    dias = {tuple(k)[1] for k in SM._ACC}
    assert len(dias) <= SM.RETAIN_DAYS, (
        f"quedan {len(dias)} días de acumulado; el tope es {SM.RETAIN_DAYS}")
    assert len(SM._ACC) == len(activos) * SM.RETAIN_DAYS
    SM.reset()


def test_el_acumulado_del_dia_en_curso_no_se_pierde_al_purgar():
    """Purgar no puede tocar lo que se está acumulando ahora mismo."""
    from app.core import session_mode as SM
    SM.reset()
    hoy = datetime(2026, 3, 16, 17, 0, tzinfo=timezone.utc)   # lunes
    for d in range(6):
        SM.accumulate("DIA", price=100.0 + d, volume=5.0,
                      now=hoy + timedelta(days=d))
    ultimo = hoy + timedelta(days=5)
    ses = SM.resolve(ultimo)
    snap = SM.snapshot("DIA", ses["mode"], ses["trading_date"], ultimo)
    assert snap is not None, "el acumulado del día en curso desapareció"
    assert snap["last"] == 105.0
    SM.reset()


# ═══════════════════════════════════════════════════════════════════════════
# 2 · LA HORA DEL SCANNER
# ═══════════════════════════════════════════════════════════════════════════

def test_ningun_modulo_de_app_sella_con_datetime_now_ingenuo():
    """Un `datetime.now()` sin zona no se puede restar de uno con zona.

    Es un `TypeError` en tiempo de ejecución, y cerca del cambio de fecha
    archiva la fila bajo el día equivocado en un servidor que no esté en UTC.
    """
    import re
    from pathlib import Path
    culpables = []
    for f in sorted(Path("app").rglob("*.py")):
        for n, linea in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if linea.lstrip().startswith("#"):
                continue
            if re.search(r"datetime\.now\(\s*\)", linea):
                culpables.append(f"{f}:{n}")
    assert not culpables, "sellan con hora local y sin zona: " + ", ".join(culpables)


def test_el_generated_at_del_scanner_lleva_zona_horaria():
    from datetime import datetime as _dt
    from pathlib import Path
    src = Path("app/core/scenario_engine.py").read_text(encoding="utf-8")
    assert '"generated_at": datetime.now(timezone.utc).isoformat()' in src
    # Y lo que sella se puede comparar con cualquier otro instante del proyecto.
    marca = _dt.now(timezone.utc).isoformat()
    assert _dt.fromisoformat(marca).tzinfo is not None
    assert (_dt.now(timezone.utc) - _dt.fromisoformat(marca)).total_seconds() >= 0


# ═══════════════════════════════════════════════════════════════════════════
# 3 · DESCRIBIR NO ES CONTAR
# ═══════════════════════════════════════════════════════════════════════════

def test_describir_un_nivel_no_avanza_su_contador_de_persistencia():
    """Medido: dos ciclos reales se publicaban como cinco.

    `audit` describe por dentro. Quien quisiera las filas para la pantalla Y el
    recuento para el Auditor —el mismo ciclo, dos lecturas— sumaba dos. Y
    `persistence.cycles` es lo que sostiene «este muro lleva en pie N ciclos»,
    que es un argumento para operar.
    """
    from app.core import level_identity as LI
    niveles = [{"kind": "call_wall", "price": 534.70, "authority": "ITMQ_WALL_ENGINE"}]
    LI.reset()
    for i in range(3):
        t = datetime(2026, 9, 21, 14, i, tzinfo=timezone.utc)
        LI.describe(niveles, symbol="DIA", now=t, cycle_id=f"DIA#{i}")
        auditoria = LI.audit(niveles, symbol="DIA", now=t, cycle_id=f"DIA#{i}")
    assert auditoria["rows"][0]["persistence"]["cycles"] == 3, (
        "describir y auditar el mismo ciclo lo contó dos veces")
    LI.reset()


def test_el_mismo_ciclo_leido_diez_veces_sigue_siendo_un_ciclo():
    from app.core import level_identity as LI
    niveles = [{"kind": "put_wall", "price": 410.0, "authority": "ITMQ_WALL_ENGINE"}]
    LI.reset()
    t = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)
    for _ in range(10):
        fila = LI.describe(niveles, symbol="SPY", now=t, cycle_id="SPY#1")[0]
    assert fila["persistence"]["cycles"] == 1
    LI.reset()


def test_un_ciclo_nuevo_si_avanza_el_contador():
    """La corrección no puede congelar el contador: eso sería el defecto opuesto."""
    from app.core import level_identity as LI
    niveles = [{"kind": "flip", "price": 5800.0, "authority": "ITMQ_WALL_ENGINE"}]
    LI.reset()
    for i in range(4):
        fila = LI.describe(niveles, symbol="SPX",
                           now=datetime(2026, 9, 21, 15, i, tzinfo=timezone.utc),
                           cycle_id=f"SPX#{i}")[0]
    assert fila["persistence"]["cycles"] == 4
    LI.reset()


def test_el_nivel_que_se_mueve_reestrena_contador_aunque_repita_ciclo():
    from app.core import level_identity as LI
    LI.reset()
    t = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)
    LI.describe([{"kind": "call_wall", "price": 100.0}], symbol="DIA",
                now=t, cycle_id="DIA#1")
    fila = LI.describe([{"kind": "call_wall", "price": 130.0}], symbol="DIA",
                       now=t, cycle_id="DIA#1")[0]
    assert fila["persistence"]["cycles"] == 1
    assert fila["persistence"]["moved_this_cycle"] is True
    LI.reset()


def test_el_trace_nombra_el_ciclo_al_auditar_la_identidad_de_niveles():
    """Si producción no nombra el ciclo, la corrección no protege a producción."""
    from pathlib import Path
    src = Path("app/main.py").read_text(encoding="utf-8")
    assert "_LI.audit(kept, symbol=symbol, cycle_id=_ciclo)" in src


# ═══════════════════════════════════════════════════════════════════════════
# 4 · LA VENTANA SE BUSCA, NO SE BARRE
# ═══════════════════════════════════════════════════════════════════════════

class _Instante(datetime):
    """Un `datetime` que cuenta cuántas veces alguien lo resta.

    Medir el TIEMPO de pared haría la prueba inestable en una máquina cargada.
    Lo que define el defecto no es el reloj: es cuántas restas de instantes se
    hacen. Eso es exacto y se puede contar.
    """
    restas = 0

    def __sub__(self, other):
        type(self).restas += 1
        return super().__sub__(other)


def _tape(n_ev: int, n_op: int):
    t0 = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
    ops = [{"timestamp": (t0 + timedelta(seconds=i * 10)).isoformat(),
            "optionType": "CALL" if i % 2 else "PUT",
            "tradeSideCode": "ASK" if i % 3 else "BID",
            "premium": 1000.0 + i, "strike": 100 + (i % 20),
            "size": 10, "price": 1.5}
           for i in range(n_op)]
    evs = [{"t": (t0 + timedelta(seconds=i * 25)).isoformat(), "v": 1.0}
           for i in range(n_ev)]
    return evs, ops


def _restas(monkeypatch, n_ev: int, n_op: int) -> int:
    from app.core import qflow
    real = qflow._parse_ts

    def contando(v):
        ts = real(v)
        if ts is None:
            return None
        return _Instante(ts.year, ts.month, ts.day, ts.hour, ts.minute,
                         ts.second, ts.microsecond, tzinfo=ts.tzinfo)

    evs, ops = _tape(n_ev, n_op)
    monkeypatch.setattr(qflow, "_parse_ts", contando)
    _Instante.restas = 0
    out = qflow.attribute_events(evs, ops)
    assert out.get("events"), "la atribución no devolvió nada; la medición no vale"
    return _Instante.restas


def test_atribuir_concentraciones_no_barre_toda_la_cinta(monkeypatch):
    """Medido antes: 195 ms con 390 concentraciones y 1.500 operaciones; 728 ms
    al doblar las dos. Cuadrático, y en el camino de refresco.

    Por CADA concentración se recorrían TODAS las operaciones para quedarse con
    las de ±90 s. La cinta ya está ordenada por instante, así que los extremos
    de la ventana se localizan por búsqueda binaria.
    """
    pequeno = _restas(monkeypatch, 390, 1500)
    grande = _restas(monkeypatch, 780, 3000)
    # Cuadrático sería x4 al doblar las dos cosas. Lineal es x2. El margen deja
    # sitio al coste real de la ventana, que sí crece, sin dejar pasar un barrido.
    assert grande <= pequeno * 2.6, (
        f"al doblar concentraciones y operaciones las restas de instantes "
        f"pasaron de {pequeno} a {grande} (x{grande / pequeno:.2f}): "
        f"sigue barriendo la cinta entera")
    # Y en términos absolutos: barrer 390 x 1.500 son 585.000 restas.
    assert pequeno < 585_000 / 10, (
        f"{pequeno} restas para 390 concentraciones x 1.500 operaciones")


def test_la_ventana_de_atribucion_sigue_siendo_la_misma(monkeypatch):
    """Buscar más rápido no puede cambiar QUÉ operaciones entran."""
    from app.core import qflow
    evs, ops = _tape(20, 200)
    out = qflow.attribute_events(evs, ops)
    t0 = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
    for fila in out["events"]:
        et = datetime.fromisoformat(fila["t"])
        esperadas = sum(
            1 for i in range(200)
            if abs(((t0 + timedelta(seconds=i * 10)) - et).total_seconds())
            <= qflow.ATTRIBUTION_WINDOW_SECONDS)
        assert fila["trades"] == esperadas, (
            f"en {fila['t']} la ventana trajo {fila['trades']} operaciones y "
            f"por definición caben {esperadas}")
