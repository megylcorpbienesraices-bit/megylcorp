from app.core import source_fusion as sf
from app.core.premarket_intelligence import build_premarket_report
from app.service import PlatformState


def test_normal_refresh_only_exposes_retained_sources(tmp_path, monkeypatch):
    class AL:
        @staticmethod
        def load_settings(): return object()
    monkeypatch.setattr(sf, "alpaca_data", AL())
    monkeypatch.setattr(sf, "quantdata_context", lambda _symbol: {
        "name":"QUANTDATA","status":"WAITING","role":"OPTIONS INTELLIGENCE",
        "source_type":"REST FEATURE SOURCE","values":{},"observed":False,
    })
    r = sf.source_context("DIA", tmp_path, 535.0, on_demand=False)
    assert set(r["sources"]) == {"alpaca_related", "quantdata", "fred", "bls", "federal_reserve"}
    assert r["sources"]["quantdata"]["role"] == "OPTIONS INTELLIGENCE"
    assert r["sources"]["fred"]["status"] == "BUILT-IN"
    assert "cboe" not in r["sources"]
    assert "chartexchange" not in r["sources"]
    assert "bookmap" not in r["sources"]


def test_external_model_agreement_is_quantdata_corroboration_only():
    fusion={"sources":{"quantdata":{"values":{"gamma":{"top_strikes":[{"strike":534.6},{"strike":537.0}]}}}}}
    r=sf.model_agreement({"call_wall":537.1,"gamma_flip":534.5},fusion)
    assert r["checked"] == 2 and r["agreed"] == 2
    assert "vote" not in r and "score" not in r
    assert all(x["source"] == "QUANTDATA" for x in r["matches"])


def test_chain_validation_is_provider_neutral():
    r=sf.chain_validation({"key_strikes":[]},{})
    assert r["status"] == "NORMALIZED CHAIN FABRIC"
    assert "native_structure_authority" in r


def test_source_fusion_cannot_flip_native_premarket_direction():
    s=PlatformState(); s.mode="DEMO"; s.refresh(True)
    base = s.analyze_premarket(False)
    original = base["summary"]["bias_raw"]
    ext={"related":{"XLI":{"valid":True,"price":150,"previous_close":145,"change_pct":3.4,"vwap":149},
                    "XLF":{"valid":True,"price":60,"previous_close":58,"change_pct":3.4,"vwap":59.5}}}
    report=build_premarket_report(symbol=s.symbol,result=s.gamma_delta,positioning=s.positioning or {},volatility=s.vol or {},scanner=s.scanner or {},expiry_confluence=s.expiry_confluence_data or {},flow=s.flow_summary or {},dealer=s.dealer_intelligence_report or {},external=ext,source_health=s.source_health_report or {},source_fusion={},macro=s.macro or {},data_quality=s.data_quality_report or {},model_health=s.model_health or {},meta=s.meta or {})
    assert report["summary"]["bias_raw"] == original
    assert abs(float(report["confirmation_summary"]["confirmation_adjustment"])) <= 8.000001


def test_asset_ecosystems_cover_all_platform_assets():
    """Todo activo del catálogo resuelve un ecosistema utilizable.

    v1.58.0 · Antes se exigía que cada activo tuviera FICHA PROPIA en
    `ASSET_ECOSYSTEMS`. Con el universo cerrado a 36 símbolos, la mayoría no
    necesita ficha: su ecosistema es él mismo y su cadena, y escribir 33 fichas
    iguales sería la tabla por activo que la arquitectura evita. Lo que sí hay
    que garantizar —y es más fuerte— es que NINGUNO se queda sin ecosistema.
    """
    from app.core.assets import ASSETS
    sin_resolver = []
    for sym in ASSETS:
        eco = sf.ecosystem_for(sym)
        if eco.get("family") == "UNSUPPORTED" or eco.get("primary_type") == "UNKNOWN":
            sin_resolver.append(sym)
    assert not sin_resolver, sin_resolver
