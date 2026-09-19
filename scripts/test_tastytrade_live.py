from __future__ import annotations
import asyncio, os, sys, time
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
LOCAL_ENV = BASE / '.env'
GLOBAL_ENV = Path.home() / '.itm_quant_gamma' / 'alpaca.env'

def read_env(path: Path) -> dict[str,str]:
    out={}
    if not path.exists(): return out
    try:
        for raw in path.read_text(encoding='utf-8').splitlines():
            line=raw.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            k,v=line.split('=',1); out[k.strip()]=v.strip().strip('"').strip("'")
    except Exception as exc: print(f"[ITM][WARN] prueba tastytrade toleró {type(exc).__name__}: {exc}", file=sys.stderr)
    return out

def load_env():
    merged={}
    for p in (GLOBAL_ENV, LOCAL_ENV): merged.update(read_env(p))
    for k,v in merged.items(): os.environ[k]=v
    return merged

async def main(symbol: str='DIA') -> int:
    env=load_env(); symbol=(symbol or 'DIA').upper().strip()
    version=(BASE/'VERSION.txt').read_text(encoding='utf-8').strip() if (BASE/'VERSION.txt').exists() else '?'
    print(f'ITM QUANT v{version} · PRUEBA REAL TASTYTRADE · {symbol}')
    print('No imprime Client Secret, Refresh Token, access token ni quote token.\n')
    required={
        'Client ID': bool(env.get('TASTYTRADE_CLIENT_ID')),
        'Client Secret': bool(env.get('TASTYTRADE_CLIENT_SECRET')),
        'Refresh Token': bool(env.get('TASTYTRADE_REFRESH_TOKEN')),
    }
    print('CREDENCIALES LOCALES')
    for k,v in required.items(): print(f'  {k:<18}: {"SI" if v else "NO"}')
    if not required['Client Secret'] or not required['Refresh Token']:
        print('\nRESULTADO: TASTYTRADE NO ESTA CONFIGURADO.')
        print('Necesita Client Secret + Refresh Token. Client ID se conserva si tu OAuth lo proporciona.')
        return 2

    sys.path.insert(0,str(BASE))
    from app.providers.tastytrade.runtime import TastytradeRuntime
    rt=TastytradeRuntime()
    try:
        ok=await rt.start(symbol)
        print('\nOAUTH / REST')
        print('  Inicio runtime      :', 'OK' if ok else 'FALLO')
        st=rt.status()
        print('  OAuth               :', st.get('oauth'))
        print('  Auth                :', st.get('auth'))
        print('  Ultimo error        :', st.get('last_error') or 'NINGUNO')
        if not ok:
            return 3

        print('\nDXLINK · esperando eventos reales hasta 30 s...')
        deadline=time.monotonic()+30.0
        last={}
        while time.monotonic()<deadline:
            st=rt.status(); rows=rt.market_data.recent(limit=10000)
            counts=Counter(str(x.get('event_type') or '').upper() for x in rows)
            last=(st,counts,rows)
            if st.get('dxlink')=='CONNECTED' and (counts.get('QUOTE',0)>0 or counts.get('TRADE',0)>0) and (counts.get('GREEKS',0)>0 or counts.get('SUMMARY',0)>0):
                break
            await asyncio.sleep(2.0)
        st,counts,rows=last if last else (rt.status(),Counter(),[])
        print('\nRESULTADO DXLINK')
        print('  Configurado         :', 'SI' if st.get('configured') else 'NO')
        print('  DXLink              :', st.get('dxlink'))
        print('  Market data         :', st.get('market_data'))
        print('  Active symbol       :', st.get('active_symbol'))
        print('  Subscripciones      :', st.get('subscriptions'))
        print('  Reconnects          :', st.get('reconnects'))
        print('  Dropped events      :', st.get('dropped_events'))
        print('  Quote events        :', counts.get('QUOTE',0))
        print('  Trade events        :', counts.get('TRADE',0))
        print('  Greeks events       :', counts.get('GREEKS',0))
        print('  Summary events      :', counts.get('SUMMARY',0))
        print('  Deriv contracts     :', (st.get('derivatives') or {}).get('contracts'))
        print('  Greek events deriv. :', (st.get('derivatives') or {}).get('greeks_events'))
        print('  Summary events der. :', (st.get('derivatives') or {}).get('summary_events'))
        try:
            tf=rt.market_data.option_trade_frame(symbol,minutes=30)
            print('  Option trades 30m   :', len(tf))
        except Exception as exc: print(f"[ITM][WARN] prueba tastytrade toleró {type(exc).__name__}: {exc}", file=sys.stderr)
        print('  Ultimo error        :', st.get('last_error') or 'NINGUNO')

        live = st.get('dxlink')=='CONNECTED'
        market = counts.get('QUOTE',0)+counts.get('TRADE',0) > 0
        deriv = counts.get('GREEKS',0)+counts.get('SUMMARY',0) > 0
        print('\nINTERPRETACION')
        if live and market and deriv:
            print('  PROBADO: tastytrade DXLink esta entregando precio + datos de derivados al proceso.')
        elif live and market:
            print('  PARCIAL: DXLink y precio probados; derivados aun sin eventos observados en esta ventana.')
        elif live:
            print('  CONECTADO: socket autorizado, pero no hubo eventos de mercado en la ventana de prueba.')
        else:
            print('  NO PROBADO: DXLink no llego a CONNECTED. Revisa OAuth/entitlement/red.')
        print('\nNOTA: fuera de actividad de mercado, cero Trade/Greeks puede ser normal. El socket CONNECTED sigue siendo una prueba de autenticacion/stream.')
        return 0 if live else 4
    finally:
        try: await rt.stop()
        except Exception as exc: print(f"[ITM][WARN] prueba tastytrade toleró {type(exc).__name__}: {exc}", file=sys.stderr)

if __name__=='__main__':
    symbol=sys.argv[1] if len(sys.argv)>1 else 'DIA'
    raise SystemExit(asyncio.run(main(symbol)))
