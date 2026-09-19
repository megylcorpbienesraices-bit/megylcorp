# Seguridad de despliegue local

ITM QUANT está diseñado para uso local en `127.0.0.1`. No exponga FastAPI directamente a Internet.

- Las credenciales Alpaca permanecen en el PC del usuario.
- El paquete no incluye API keys reales.
- La autenticación vigente usa `core/net_guard.py` y secretos `ITM_*`; el módulo SQLite de login legado fue retirado en v1.40.0.
- Si en el futuro se publica detrás de una red/servidor, use autenticación real, HTTPS, firewall/reverse proxy, secretos externos y rotación de credenciales.
