from pathlib import Path
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT=Path(__file__).resolve().parents[1]


def text(path):
    return (ROOT/path).read_text(encoding='utf-8')


def test_version_and_product_marker():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')


def test_canonical_ecosystems_cover_tactical_assets():
    from app.core.assets import ASSETS, asset_info
    for sym,cfg in ASSETS.items():
        info=asset_info(sym)
        eco=info['ecosystem']
        assert eco['symbol']==sym
        # v1.42: la arquitectura es MULTI_ASSET; el ecosistema Dow es una familia, no el producto
        # al estrecharse el alcance. Contrato completo en tests/test_v1271_dow_scope_contract.py.
        assert eco['architecture']=='MULTI_ASSET · SEPARATE INSTRUMENT MATH'
        assert set(eco['derivatives'])=={'equity_options','index_options','future_options'}
        if cfg.get('full'):
            # full means OWN derivative-chain support; a future relationship is optional
            assert any(sym in eco['derivatives'][family] for family in ('equity_options','index_options','future_options'))


def test_dow_ecosystem_roots_are_correct():
    """v1.27.1: reemplaza a `test_major_ecosystem_roots_are_correct`.

    SPY/QQQ/GLD/VXX salieron del universo en v1.27.0. Además el índice de DIA es
    ahora DJX y no DJI: DJX (DJIA/100) tiene cadena de opciones, DJI es el índice
    crudo y no la tiene, así que devolverlo invitaba a pedir una cadena inexistente.
    Contrato completo del alcance en tests/test_v1271_dow_scope_contract.py.
    """
    from app.core.asset_ecosystems import public_summary
    dia = public_summary('DIA')
    assert {'YM', 'MYM'} <= set(dia['futures'])
    assert 'DJX' in dia['indices'] and 'DJI' not in dia['indices']
    # Fuera del universo: degrada de forma explícita, no inventa relaciones.
    assert public_summary('SPY')['family'] == 'UNSUPPORTED'


def test_tastytrade_resolves_provider_futures_and_all_derivative_families():
    src=text('app/providers/tastytrade/symbols.py')
    assert 'front_future(product)' in src
    assert 'index_options' in src
    assert 'future_options' in src
    assert 'derivative_max_contracts' in src
    assert 'contract month symbols are never hand-built' in src


def test_ecosystem_runtime_is_nonblocking_and_normalized():
    src=text('app/core/ecosystem_runtime.py')
    assert 'asyncio.to_thread(alpaca_data.fetch_stock_snapshot' in src
    assert 'SEPARATE_INSTRUMENT_RETURNS_THEN_ROLE_NORMALIZED_FUSION' in src
    assert 'NO_FIXED_PROVIDER_RANK_QUALITY_BY_OBSERVATION' in src
    assert 'ECOSYSTEM_RUNTIME = AssetEcosystemRuntime()' in src


def test_derivative_structure_is_observed_not_fake_dealer_gex():
    src=text('app/providers/tastytrade/market_data.py')
    assert 'def derivative_feature' in src
    assert 'OBSERVED_DERIVATIVE_STRUCTURE_NOT_DEALER_POSITION_ASSUMPTION' in src
    assert 'observed_delta_oi_balance' in src
    assert 'gamma_oi_intensity' in src


def test_feature_bus_and_scanner_accept_new_semantic_channels():
    bus=text('app/core/provider_bus.py')
    scan=text('app/core/scenario_engine.py')
    assert '"ecosystem", "derivatives"' in bus
    assert '_fuse_signal_channel(0, 0, provider_features, "ecosystem")' in scan
    assert '_fuse_signal_channel(0, 0, provider_features, "derivatives")' in scan
    assert 'provider_ecosystem_confidence' in scan
    assert 'provider_derivative_confidence' in scan


def test_main_starts_runtime_and_exposes_ecosystem_diagnostics():
    src=text('app/main.py')
    assert 'ECOSYSTEM_RUNTIME.start(STATE.symbol)' in src
    assert 'ECOSYSTEM_RUNTIME.select_asset(target)' in src
    assert '@app.get("/api/assets/ecosystem")' in src
    assert '"asset_ecosystem": ECOSYSTEM_RUNTIME.status()' in src


def test_ui_surfaces_etf_index_future_derivatives():
    html=text('app/templates/dashboard.html')
    js=text('app/static/app.js')
    for token in ['ecoEquities','ecoIndices','ecoFutures','ecoDerivatives','ecoFusionState']:
        assert token in html
        assert token in js
    assert 'id="symbolSearchModal"' in html
    assert 'ecosystem-dock lean-hidden' in html


def test_library_expands_related_alpaca_components_without_hot_subscription_explosion():
    src=text('app/core/provider_library.py')
    assert 'ECOSYSTEM_RELATED_SNAPSHOT' in src
    assert 'ECOSYSTEM_OPTION_CONTRACT_CATALOG' in src
    assert 'one related ETF/equity root per WARM/COLD' in src


def test_tradestation_is_an_explicit_observed_capability_provider():
    lake=text('app/core/provider_data_lake.py')
    assert 'TRADESTATION' not in lake


def test_tasty_derivative_feature_uses_observed_delta_oi_balance_without_fake_gex():
    from app.providers.tastytrade.health import TastytradeHealth
    from app.providers.tastytrade.market_data import TastytradeMarketData
    md=TastytradeMarketData(TastytradeHealth())
    md._derivative_state={"DIA":{
        "C1":{"GREEKS":{"payload":{"delta":0.60,"gamma":0.02,"option_type":"call","instrument_type":"EQUITY_OPTION"}},"SUMMARY":{"payload":{"open_interest":100,"option_type":"call","instrument_type":"EQUITY_OPTION"}}},
        "P1":{"GREEKS":{"payload":{"delta":-0.35,"gamma":0.02,"option_type":"put","instrument_type":"EQUITY_OPTION"}},"SUMMARY":{"payload":{"open_interest":50,"option_type":"put","instrument_type":"EQUITY_OPTION"}}},
    }}
    out=md.derivative_feature("DIA")
    assert out["contracts_ready"]==2
    assert out["directional"]["derivatives"]["sign"]==1
    assert out["call_oi"]==100 and out["put_oi"]==50
    assert "dealer" in out["authority"].lower()
    assert "gex" not in out


def test_ecosystem_feature_can_fuse_index_future_related_without_raw_price_addition():
    from app.core.provider_bus import PROVIDER_BUS
    from app.core.ecosystem_runtime import AssetEcosystemRuntime
    from datetime import datetime, timezone
    now=datetime.now(timezone.utc)
    # DIA ecosystem: XLI/XLF, DJI, YM. Values are intentionally on incompatible price scales.
    for sym,px,base in [('XLI',150,149),('XLF',55,54.5),('DJI',47000,46800),('YM',47025,46820)]:
        PROVIDER_BUS.ingest(source='TEST_A',symbol=sym,event_type='SNAPSHOT',values={'price':px,'prev_close':base},timestamp=now,received_at=now)
    rt=AssetEcosystemRuntime()
    f=rt._build_feature('DIA')
    assert f['directional']['ecosystem']['sign']==1
    assert f['groups']['future']['components'][0]['symbol']=='YM'
    assert f['fusion_rule']=='SEPARATE_INSTRUMENT_RETURNS_THEN_ROLE_NORMALIZED_FUSION'
