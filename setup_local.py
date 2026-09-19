from __future__ import annotations
import getpass
from pathlib import Path

from setup_local_gui import existing_settings, save_settings

old = existing_settings()
print('\nITM QUANT — Centro de proveedores\n')
print('Todos los proveedores configurados participan en la arquitectura común; no hay rango fijo principal/secundario/terciario.\n')

def keep_or_input(label: str, current: str = '', secret: bool = False) -> str:
    suffix = ' [Enter = conservar]' if current else ''
    if secret:
        value = getpass.getpass(f'{label}{suffix}: ').strip()
    else:
        value = input(f'{label}{suffix}: ').strip()
    return current if not value and current else value

values = {
    'ALPACA_API_KEY': keep_or_input('Alpaca API Key', old.get('ALPACA_API_KEY','')),
    'ALPACA_SECRET_KEY': keep_or_input('Alpaca Secret Key', old.get('ALPACA_SECRET_KEY',''), True),
    'QUANTDATA_API_KEY': keep_or_input('Quant Data API Key', old.get('QUANTDATA_API_KEY',''), True),
    'QUANTDATA_BASE_URL': old.get('QUANTDATA_BASE_URL','https://api.quantdata.us'),
    'TASTYTRADE_CLIENT_ID': keep_or_input('Tastytrade Client ID', old.get('TASTYTRADE_CLIENT_ID','')),
    'TASTYTRADE_CLIENT_SECRET': keep_or_input('Tastytrade Client Secret', old.get('TASTYTRADE_CLIENT_SECRET',''), True),
    'TASTYTRADE_REFRESH_TOKEN': keep_or_input('Tastytrade Refresh Token', old.get('TASTYTRADE_REFRESH_TOKEN',''), True),
    'TASTYTRADE_BASE_URL': old.get('TASTYTRADE_BASE_URL','https://api.tastyworks.com'),
}
save_settings(values)
print('\nConfiguración guardada localmente sin borrar otros parámetros del .env.')
print('Reinicia ITM QUANT. No compartas el archivo .env.\n')
