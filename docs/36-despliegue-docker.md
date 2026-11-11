# 36 · Despliegue con Docker

Guía para levantar el multi-cotizador con Docker: primero en una laptop Windows (WSL2) para
probar y después en un servidor Linux (VM de Azure o el servidor del socio). Los mismos
archivos sirven en todos lados; lo único que cambia es el `.env` de cada instalación.

No contiene contraseñas ni datos reales. El `.env` nunca entra a la imagen ni a Git.

## 1. Cómo está armado

```
navegador ──► proxy (nginx, puerto publicado) ──► web (uvicorn + Django) ──► db (PostgreSQL 18)
                 │ sirve /static/                    │ PDFs originales
                 ▼                                   ▼
          (dentro de la imagen)               volumen «cotizaciones»       volumen «postgres»
```

| Archivo | Para qué |
| --- | --- |
| `Dockerfile` | Construye la imagen de la app (`app`) y la del proxy con los estáticos (`proxy`) |
| `docker-compose.yml` | Levanta juntos `db`, `web` y `proxy`, con sus volúmenes |
| `deploy/nginx.conf` | Configuración de nginx: estáticos, tope de 2.5 MB y encabezados para Django |
| `.dockerignore` | Deja fuera de la imagen el `.env`, `venv/`, `.git`, datos locales y notas |

Solo `proxy` publica un puerto. `web` y `db` no son accesibles desde fuera de Docker.

## 2. Preparar una laptop Windows (una vez)

1. PowerShell como administrador: `wsl --install` y reiniciar. Al abrir Ubuntu, responder `n`
   a «platform metrics» y crear el usuario de Linux (la contraseña no se ve al escribirla).
2. `sudo apt update && sudo apt upgrade -y`.
3. Instalar Docker Desktop con «Use WSL 2». En **Settings → Resources → WSL Integration**,
   activar Ubuntu.
4. Si `docker run hello-world` dice `permission denied ... docker.sock`:
   `sudo usermod -aG docker $USER`, luego en PowerShell `wsl --shutdown` y abrir Ubuntu otra vez.
5. Clonar el repo **dentro de Ubuntu** (no en `C:\`): es más rápido y evita saltos de línea de
   Windows.

   ```bash
   mkdir -p ~/proyectos && cd ~/proyectos
   git clone https://github.com/NauRedfieldev/Cotizador_Multiproveedor.git
   cd Cotizador_Multiproveedor
   ```

## 3. Primer arranque

Todos los comandos se ejecutan en la carpeta del repo.

### 3.1 Crear el `.env`

```bash
cp .env_example .env
python3 -c "import secrets; print(secrets.token_urlsafe(50))"   # copiar el resultado a SECRET_KEY
```

Editar `.env` (por ejemplo con `code .env` o `nano .env`):

| Variable | Prueba en la laptop | Servidor |
| --- | --- | --- |
| `SECRET_KEY` | La generada arriba | Una propia del servidor, distinta |
| `DEBUG` | `False` | `False` |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | IP o dominio del servidor |
| `USE_HTTPS` | `False` | `True` solo cuando ya haya HTTPS (§6) |
| `NAME_DB`, `USER_DB`, `PASSWORD_DB` | Inventados (p. ej. `cotizador`, `cotizador`, una contraseña larga) | Propios del servidor |
| `CCONOR_EMPRESA_NAME` | Un nombre de prueba | Los datos oficiales (docs/30 A4) |
| `COTIZACIONES_EMISION_HABILITADA` | `False` | Solo tras B7 (docs/30 A9) |
| `*_SMTP_HOST` | Vacíos: el correo solo sale en el log | Los reales (docs/30 A4c/A4d) |
| `PUBLICAR_HTTP` | Sin poner (= `127.0.0.1:8080`) | `80` |

- `HOST_DB`, `PORT_DB` y `QUOTE_PDF_ROOT` del `.env` no cuentan en Docker: los fija
  `docker-compose.yml` (`db`, `5432` y el volumen).
- Un valor que lleve `$` va entre comillas simples (`PASSWORD_DB='abc$123'`); si no, Docker
  intenta reemplazarlo y avisa «variable is not set».
- En la laptop de prueba, usar datos falsos. No copiar ahí el `.env` real.

### 3.2 Construir y crear la base

```bash
docker compose build                                     # 1.ª vez tarda unos minutos
docker compose up -d db                                  # solo PostgreSQL
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py createsuperuser
```

Grupos comerciales (docs/30 A5), con el usuario que se acaba de crear:

```bash
docker compose run --rm web python manage.py shell -c "from django.contrib.auth import get_user_model; from apps.quotes.permissions import configure_commercial_groups; configure_commercial_groups(actor=get_user_model().objects.get(username='TU_USUARIO'))"
```

### 3.3 Levantar todo

```bash
docker compose up -d
docker compose ps          # los tres en «running» (db además «healthy»)
```

Abrir en el navegador de Windows: <http://localhost:8080>. Debe aparecer el login.

### 3.4 Proveedores

El alta de proveedores es por instalación (Git no la trae). En `/admin/` → Providers, como en
[docs/17 §7](17-adaptador-syscom.md) (SYSCOM) y [docs/32 §8](32-scraper-exel-web.md) (Exel).

- Las credenciales (`PROVIDER_*`) van en el `.env`. Tras cambiarlas: `docker compose up -d`.
- **SYSCOM:** la instalación nueva pide su propio token en la primera búsqueda. No repetir
  búsquedas en bucle ni borrar el token (límite de SYSCOM, docs/17 §7). En una prueba sin
  necesidad de precios reales, dejar el proveedor inactivo.

## 4. Uso diario

| Qué | Comando |
| --- | --- |
| Ver si está arriba | `docker compose ps` |
| Ver errores de la app | `docker compose logs -f web` (Ctrl+C para salir) |
| Ver peticiones del proxy | `docker compose logs -f proxy` |
| Apagar (los datos se quedan) | `docker compose down` |
| Encender | `docker compose up -d` |
| Actualizar a la versión nueva | `git pull`, respaldar (§5), luego `docker compose up -d --build`; si hay migraciones, `docker compose run --rm web python manage.py migrate` |

> ⚠️ **Nunca** `docker compose down -v`: la `-v` borra los volúmenes, es decir, **la base de
> datos y los PDFs**.

- Los errores 500 salen completos en `docker compose logs web` (variable `LOG_A_CONSOLA`).
- Las migraciones nunca corren solas al arrancar: siempre después de un respaldo (docs/29).
- Si cambias `.env`, basta `docker compose up -d` (vuelve a crear `web` con los valores nuevos).
- Cambiar `NAME_DB`/`USER_DB`/`PASSWORD_DB` después del primer arranque **no** cambia la base
  ya creada: esos valores solo cuentan al crear el volumen.

## 5. Respaldo y restauración en Docker

Mismas reglas que [docs/29](29-respaldo-y-restauracion.md): primero la BD, después los PDFs,
siempre juntos y fuera de la carpeta del repo.

```bash
mkdir -p ~/respaldos-cconor && chmod 700 ~/respaldos-cconor
STAMP=$(date +%Y%m%d-%H%M%S)
# 1) BD
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > ~/respaldos-cconor/cconor-db-$STAMP.dump
# 2) PDFs originales
docker compose exec -T web tar -C /data -czf - cotizaciones > ~/respaldos-cconor/cconor-pdf-$STAMP.tar.gz
```

Restaurar (sobre una instalación con `web` apagado: `docker compose stop web proxy`):

```bash
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < ~/respaldos-cconor/cconor-db-<FECHA>.dump
docker compose run --rm -T --entrypoint tar web -C /data/cotizaciones --strip-components=1 -xzf - < ~/respaldos-cconor/cconor-pdf-<FECHA>.tar.gz
docker compose up -d
```

Un respaldo de la instalación sin Docker (docs/29 §4) se restaura con los mismos comandos:
`--strip-components=1` quita la carpeta de arriba del `.tar.gz`, se llame `cotizaciones` o
`private_quote_files`.

## 6. En un servidor (VM de Azure o del socio)

1. Instalar Docker Engine en Linux (no hace falta Docker Desktop) y clonar el repo.
2. `.env` propio del servidor (§3.1, columna «Servidor») con `PUBLICAR_HTTP=80` y
   `ALLOWED_HOSTS` con su IP o dominio.
3. §3.2 a §3.4 igual.
4. **Mientras no haya HTTPS:** en el firewall (en Azure, el «grupo de seguridad de red») abrir
   el puerto 80 solo a tu IP y usar datos de prueba. Sin HTTPS la contraseña viaja sin cifrar.

Pendiente de decidir antes de producción (con el socio y CCONOR):

- **HTTPS:** dominio y quién lo da (Caddy/certbot en la misma VM, el proxy del socio o
  Cloudflare). Con HTTPS, `USE_HTTPS=True`.
- **Quién puede entrar:** además del login, una puerta antes de la página (IP fija de la
  oficina en el proxy, Cloudflare Access o VPN).
- **Proxy del socio delante de este nginx:** este nginx toma la IP y el `https` de la
  conexión que recibe. Si hay otro proxy delante, nginx vería la IP de ese proxy y `http`.
  Hay que ajustar `deploy/nginx.conf` para confiar solo en ese proxy (su IP o red); si no,
  los límites por IP se comparten y con `USE_HTTPS=True` la redirección no termina
  (`settings/proxy.py` lo avisa en el log).
- **PostgreSQL:** el de este compose o uno del socio, y quién hace y prueba los respaldos (B7).
- **Salida a internet:** el servidor debe llegar a SYSCOM, Exel y al SMTP del correo.

## 7. Decisiones (y por qué)

| Decisión | Por qué |
| --- | --- |
| Estáticos dentro de la imagen `proxy`, no en un volumen | Un volumen conserva el CSS/JS viejo después de actualizar |
| Un solo proceso de uvicorn | Las cachés son LocMem (por proceso): con varios, cada uno tendría sus propios límites de login/búsquedas y su tipo de cambio |
| `FORWARDED_ALLOW_IPS="*"` en `web` | Seguro solo porque `web` no publica su puerto y nginx **reemplaza** `X-Forwarded-For`; si se publica el 8000, poner la IP del proxy |
| `proxy_set_header Host $http_host` | Conserva el puerto (`localhost:8080`); sin él Django rechaza los POST por CSRF |
| Migraciones a mano | Siempre después de un respaldo (docs/29); un arranque automático podía migrar sin respaldo |
| Volumen de PostgreSQL 18 en `/var/lib/postgresql` | Desde la 18 los datos viven en `/var/lib/postgresql/18/docker`; montar en `.../data` los dejaría fuera del volumen |
| App sin usuario administrador (`cotizador`) | Si alguien lograra ejecutar algo en la app, no tendría permisos de administrador en el contenedor |
| `.env` fuera de la imagen | La imagen puede copiarse o subirse; los secretos solo viven en el servidor |
| `STATIC_ROOT` y `LOG_A_CONSOLA` en `settings.py` | collectstatic necesita la carpeta destino; con `DEBUG=False` Django no escribía la traza de un 500 en ningún lado |

## 8. Problemas comunes

| Síntoma | Causa y arreglo |
| --- | --- |
| `permission denied ... docker.sock` | Usuario fuera del grupo `docker` (§2 paso 4) |
| `Falta NAME_DB en .env` al arrancar | Falta `.env` en la carpeta del repo o esa variable vacía |
| `The "xyz" variable is not set` | Un valor con `$` sin comillas simples (§3.1) |
| La página da **400 Bad Request** | El dominio/IP con el que entras no está en `ALLOWED_HOSTS` |
| **403** al iniciar sesión (CSRF) | Se entró por otro dominio o puerto que el del navegador; revisar `ALLOWED_HOSTS` y no cambiar `Host $http_host` en nginx |
| **502 Bad Gateway** | `web` está caído o arrancando: `docker compose logs web` |
| **500** | La traza completa sale en `docker compose logs web` |
| Página sin estilos | Se cambiaron estáticos sin reconstruir: `docker compose up -d --build` |
| Redirección infinita con `USE_HTTPS=True` | Hay HTTPS antes de nginx que no se reenvía como `https` (§6, proxy del socio) |
| `connection refused` a la BD al migrar | `db` aún no está «healthy»: esperar unos segundos y repetir |
| `password authentication failed for user …` | El volumen de PostgreSQL se creó con otro usuario/contraseña (se arrancó `db` antes de terminar el `.env` o se cambió después). Si es una instalación **nueva sin datos**: `docker compose down`, `docker volume rm cconor-cotizador_postgres`, `docker compose up -d db` y repetir `migrate`. Con datos: cambiar la contraseña dentro de PostgreSQL, nunca borrar el volumen |
