param(
  [string]$Python = "python",
  [string]$LocalModel = "qwen3:8b",
  [switch]$InstallOllama,
  [switch]$PullModel
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Lock = Join-Path $Root "requirements.sophia-local.lock.txt"

Write-Host "ITM QUANT · Sophia local runtime" -ForegroundColor Cyan
Write-Host "Ruta opcional local; no usa claves cloud ni créditos por consulta." -ForegroundColor DarkGray

if (-not (Test-Path $Lock)) {
  throw "Sophia local está fail-closed: falta requirements.sophia-local.lock.txt con versiones y hashes revisados. requirements-sophia-local.in es solo entrada del resolver y NO se instala directamente."
}

& $Python -m pip install --require-hashes --no-deps --only-binary=:all: -r $Lock
if ($LASTEXITCODE -ne 0) { throw "Falló la instalación hash-pinned de Sophia local." }

if ($InstallOllama) {
  throw "Instalación automática de Ollama deshabilitada en la ruta certificable. Instálalo/verifícalo fuera del release y vuelve a ejecutar sin -InstallOllama."
}
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  Write-Host "Ollama no está instalado. El STT local puede prepararse, pero el LLM local requiere un runtime instalado/verificado externamente." -ForegroundColor Yellow
}

if ($PullModel) {
  throw "Descarga automática de modelos Ollama deshabilitada en la ruta certificable. Fija y verifica el modelo fuera del release; luego configura ITM_SOPHIA_LLM_URL a localhost."
}

Write-Host "Dependencias Python de Sophia local instaladas desde lock hash-pinned." -ForegroundColor Green
Write-Host "Modelo solicitado (no descargado por este script): $LocalModel" -ForegroundColor DarkGray
Write-Host "Configura ITM_SOPHIA_LLM_URL a localhost cuando el runtime/modelo local verificado esté levantado." -ForegroundColor Yellow
