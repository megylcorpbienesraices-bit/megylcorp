# ITM QUANT SolidJS Tactical Shell · REFERENCE / OPTIONAL

Este árbol SolidJS conserva una propuesta de shell para estado agregado de baja frecuencia (decision, quality, market/dealer state e integraciones analíticas). **No forma parte del runtime de producción actual de ITM QUANT v1.40.0**: ningún template/ruta carga su bundle y el contenedor de producción no lo copia.

El hot path operativo permanece en el terminal nativo: `binary stream -> TypedArray/ring buffer -> Canvas/WebGPU`. High-rate market ticks **never enter SolidJS/DOM**; El evento `itmq:aggregate-ui` se conserva como contrato de integración, pero su existencia no convierte Solid en runtime activo.

Si este shell se activa en una release futura, primero debe existir un `package-lock.json` revisado y el build debe ser reproducible con `npm ci` + `npm run build`. No se permite `npm install` para resolver dependencias durante certificación.

Mientras no sea servido por la aplicación, SolidJS es **REFERENCE/OPTIONAL** y no bloquea la certificación del runtime Python/JS nativo.
