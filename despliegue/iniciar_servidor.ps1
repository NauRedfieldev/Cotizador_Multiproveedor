# Arranca el cotizador en un servidor Windows (docs/30 A12).
#
#   powershell -ExecutionPolicy Bypass -File despliegue\iniciar_servidor.ps1 [-Puerto 8000] [-IpProxy 127.0.0.1]
#
# Por que asi:
# - --workers 1: los limites (login, recuperar contrasena, busquedas, PDF y correo) viven en la memoria de
#   cada proceso; con varios workers cada uno tendria su propio contador.
# - --host 127.0.0.1: solo el proxy o el hosting (que pone el certificado HTTPS) llega a uvicorn.
# - --forwarded-allow-ips: uvicorn solo cree la IP real del cliente y el https que manda el proxy si
#   viene de esta IP. Si no coincide, todos comparten la IP del proxy en los limites y USE_HTTPS no
#   termina de redirigir; settings/proxy.py lo avisa en el log.
# Para dejarlo siempre encendido: Programador de tareas > "Al iniciar el sistema", con este comando.
param(
    [int]$Puerto = 8000,
    [string]$IpProxy = "127.0.0.1",
    [string]$Escucha = "127.0.0.1"
)
$ErrorActionPreference = "Stop"

$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

if (-not (Test-Path (Join-Path $raiz ".env"))) {
    throw "Falta .env en $raiz : copia .env_example y rellenalo (docs/30 A10)."
}
$python = Join-Path $raiz "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Falta el venv: python -m venv venv ; venv\Scripts\pip install -r requirements.txt"
}

Write-Host "Cotizador en http://${Escucha}:$Puerto (1 worker; proxy de confianza: $IpProxy)"
& $python -m uvicorn settings.asgi:application --host $Escucha --port $Puerto --workers 1 `
    --proxy-headers --forwarded-allow-ips $IpProxy
