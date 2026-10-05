# AGENTS.md

Cotizador multiproveedor — backend Django 6.1.1 + PostgreSQL (psycopg2-binary). Sin README, CI, linter ni pytest. `catalog` y `quotes` ya tienen modelos con migración `0001_initial` (DER y diccionario de datos en `docs/DER.md`); `providers` sigue en esqueleto.

## Estructura no estándar

- El paquete de configuración del proyecto se llama `settings/` (no un dir con nombre de proyecto). Módulo de settings: `settings.settings`; URLs raíz: `settings.urls`; WSGI/ASGI: `settings.wsgi` / `settings.asgi`. **No** es un paquete de settings por entorno (dev/prod).
- Las apps viven bajo el paquete `apps/`: `catalog`, `providers`, `quotes`, registradas en `INSTALLED_APPS` como `apps.catalog`, etc. Para crear una app nueva:
  1. `python manage.py startapp nombre apps\nombre`
  2. En `apps/nombre/apps.py` corregir `name = 'apps.nombre'` (startapp lo deja mal)
  3. Registrar `'apps.nombre'` en `INSTALLED_APPS`

## Setup (obligatorio antes de correr nada)

1. Usar el venv existente en `venv/`: `.\venv\Scripts\Activate.ps1` o invocar `.\venv\Scripts\python.exe` directamente. **No** usar el Python 3.13 global: tiene Django pero le faltan deps (p.ej. django-environ). Para reinstalar: `pip install -r requirements.txt`.
2. Copiar `.env_example` a `.env` y rellenar: `SECRET_KEY`, `DEBUG`, `NAME_DB`, `USER_DB`, `PASSWORD_DB`, `HOST_DB`, `PORT_DB`.
   - Sin `.env` el proyecto **no arranca**: `SECRET_KEY` no tiene default y django-environ lanza `ImproperlyConfigured`.
3. Requiere un servidor PostgreSQL accesible con la BD creada.

## Comandos

```powershell
python manage.py runserver                  # dev (WSGI)
uvicorn settings.asgi:application           # alternativa ASGI (uvicorn está en requirements)
python manage.py makemigrations; python manage.py migrate
python manage.py test                       # todos los tests (runner estándar de Django, no hay pytest)
python manage.py test apps.catalog          # tests de una sola app
```

## Gotchas verificados

- Shell del entorno: PowerShell 5.x — **`&&` no funciona**; usar `;` o comandos separados.
- Django 6.1 **conecta a la BD incluso para `makemigrations`** (check_consistent_history): sin PostgreSQL accesible, falla. `manage.py check` sí funciona sin BD (basta tener las env vars definidas).
- Dependencias ya instaladas pero aún sin usar en el código (planificadas): `reportlab` (PDF), `pillow` (imágenes), `RapidFuzz` (matching difuso), `httpx` (HTTP cliente).
- Email configurado con el setting `MAILERS` (estilo nuevo de Django 6.x) y backend de consola.
