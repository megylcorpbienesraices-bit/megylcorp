# Sophia · Quant Copilot self-hosted

## Regla de costo

Sophia está diseñada para **no depender de créditos, tokens pagados ni cobro por pregunta**. ITM QUANT no contiene ninguna conexión a OpenAI, Anthropic u otro proveedor de IA de pago para Sophia.

La arquitectura es:

`HOT STATE / READY SESSION -> Sophia Core -> modelo local opcional -> texto/voz local`

El adaptador de modelo rechaza hosts remotos: `ITM_SOPHIA_LLM_URL` solo se considera válido si apunta a `127.0.0.1`, `localhost` o `::1`.

## Qué puede leer

En una pregunta del usuario, Sophia recibe un snapshot completo y acotado de los módulos de ITM QUANT, incluyendo:

- Scanner y su fuerza/estado;
- TRACE / timing gate / order flow;
- Gamma, Delta, GEX/DEX y migraciones;
- Aggression Trigger 1m/3m/5m/15m;
- Flujo y Flujo Inusual;
- niveles/targets/Command Center;
- Volatilidad y Positioning;
- Chain Insights, Dealer Intelligence y Market State;
- derivados, expiraciones y estructura;
- macro y Large Prints;
- contexto LIVE o sesión histórica seleccionada.

Los objetos grandes (matrices/superficies/raw history) se resumen antes de entrar al contexto del modelo local para conservar latencia y RAM.

**Scanner permanece como única autoridad direccional.** Sophia explica, conecta evidencia y vigila condiciones; no fabrica señales ni datos.

## Watch Rules event-driven

Órdenes naturales como estas crean reglas internas:

- “Sophia, avísame cuando haya Flujo Inusual.”
- “Avísame si aparece Flujo Inusual comprador/vendedor.”
- “Avísame cuando la agresión de 1m cambie a compra.”
- “Avísame cuando el precio llegue al 525.”
- “Avísame cuando Scanner cambie a venta / llegue a fuerza 80.”
- “Avísame cuando Gamma y Delta se alineen.”
- “¿Qué estás vigilando?”
- “Cancela ese aviso.” / “Cancela todos los avisos.”

Las reglas escuchan el HOT STATE y el Event Bus; **no consultan un LLM repetidamente**. El flujo de opciones se consume incrementalmente por `fabric_seq`, por lo que múltiples prints rápidos no quedan ocultos por el último evento. Al crear un watch de Flujo Inusual se guarda el cursor actual para que un evento viejo no dispare un aviso nuevo.

Las reglas intradía expiran por sesión de Nueva York, salvo que en el futuro se configure explícitamente una regla persistente. Flujo Inusual es persistente durante la sesión y alerta eventos distintos; niveles/Scanner/agresión son normalmente one-shot.

## Voz

La UI incorpora:

- texto;
- Push-to-Talk;
- transcripción local;
- respuesta por texto;
- respuesta por voz ON/OFF;
- notificaciones de Watch Rules aunque el panel de Sophia esté cerrado.

STT recomendado: `faster-whisper` local, con fallback `whisper.cpp`.

TTS: Windows SAPI local en Windows Server o Piper local opcional.

No se usa Web Speech cloud ni un servicio de voz de pago.

## Modelo local

El código soporta un endpoint local tipo Ollama o llama.cpp/OpenAI-compatible. El binario/modelo no se empaqueta dentro del ZIP porque pesa varios GB y debe instalarse en la infraestructura de destino. Eso es un paso de despliegue, no un desarrollo pendiente de ITM QUANT.

Variables principales:

- `ITM_SOPHIA_LLM_URL`
- `ITM_SOPHIA_LLM_MODEL`
- `ITM_SOPHIA_STT_MODEL`
- `ITM_SOPHIA_WHISPER_CLI`
- `ITM_SOPHIA_WHISPER_MODEL_PATH`
- `ITM_SOPHIA_PIPER_CLI`
- `ITM_SOPHIA_PIPER_MODEL`

## Nota de navegador/VPS

Fuera de `localhost`, los navegadores modernos exigen **HTTPS** para habilitar `getUserMedia()` y el micrófono. El código ya está preparado; durante el despliegue VPS se configurará HTTPS/reverse proxy antes de activar Push-to-Talk desde PC, iPhone o iPad.
