# syntax=docker/dockerfile:1
# Imagen del multi-cotizador (guía: docs/36-despliegue-docker.md). Se construye por partes:
#   base       Python 3.14 + dependencias + código. Sin .env ni datos locales (ver .dockerignore).
#   estaticos  junta los estáticos con collectstatic (solo se usa para armar «proxy»).
#   proxy      nginx con esos estáticos dentro; pasa lo demás a la app.
#   app        la app con uvicorn (ASGI), con un usuario sin permisos de administrador.
# docker-compose.yml construye «proxy» y «app»: no hace falta construirlos a mano.

FROM python:3.14-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .


FROM base AS estaticos
# settings.py no carga sin estas variables. Aquí no hay BD ni secretos: solo se copian archivos.
RUN SECRET_KEY=solo-para-collectstatic NAME_DB=x USER_DB=x PASSWORD_DB=x \
    python manage.py collectstatic --noinput


FROM nginx:stable-alpine AS proxy
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
# Dentro de la imagen y no en un volumen: un volumen conservaría el CSS/JS viejo tras actualizar.
COPY --from=estaticos /app/staticfiles /srv/static


FROM base AS app
# /data/cotizaciones = QUOTE_PDF_ROOT (PDFs originales, volumen privado). Se crea aquí con su dueño
# para que el volumen nuevo herede los permisos.
RUN useradd --system --no-create-home --uid 10001 cotizador \
 && mkdir -p /data/cotizaciones \
 && chown cotizador:cotizador /data/cotizaciones \
 && chmod 700 /data/cotizaciones
USER cotizador
EXPOSE 8000
# Un solo proceso a propósito: las cachés son LocMem (memoria de cada proceso). Con varios, cada uno
# llevaría sus propios límites de login/búsquedas y su propio tipo de cambio.
# --limit-concurrency: arriba de 100 peticiones a la vez responde 503 en lugar de saturarse.
CMD ["uvicorn", "settings.asgi:application", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--limit-concurrency", "100"]
