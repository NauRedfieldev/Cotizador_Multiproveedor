"""
ASGI config for multiprovider project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings.settings')

django_application = get_asgi_application()

from django.conf import settings  # noqa: E402  (después de get_asgi_application, que carga la configuración)

RESPUESTA_413 = b"La peticion es demasiado grande."


async def _responder_413(send):
    await send({"type": "http.response.start", "status": 413, "headers": [
        (b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(RESPUESTA_413)).encode()),
        (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": RESPUESTA_413})


class LimiteDeCuerpo:
    """Corta con 413 las peticiones cuyo cuerpo pasa de DATA_UPLOAD_MAX_MEMORY_SIZE (pista 15 de la auditoría).

    Django, bajo ASGI, lee el cuerpo completo (y lo pasa a un archivo temporal arriba de 2.5 MB) antes de
    revisar el host, la sesión o la ruta, sin tope: cualquiera podía mandar un cuerpo enorme a cualquier
    URL y llenar el disco temporal. Aquí se corta por Content-Length antes de leer nada y, si no lo trae
    o miente, al pasar el tope mientras llega. La app no recibe archivos: lo más grande es el JSON de una
    cotización, muy por debajo del tope. El tiempo de lectura y las conexiones simultáneas los limita el
    proxy o uvicorn en el despliegue.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or settings.DATA_UPLOAD_MAX_MEMORY_SIZE is None:
            return await self.app(scope, receive, send)
        maximo = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        largo = dict(scope.get("headers") or []).get(b"content-length")
        if largo is not None and (not largo.strip().isdigit() or int(largo) > maximo):
            return await _responder_413(send)

        recibidos, excedido, respondio = 0, False, False

        async def recibir():
            nonlocal recibidos, excedido
            mensaje = await receive()
            if mensaje["type"] == "http.request":
                recibidos += len(mensaje.get("body", b""))
                if recibidos > maximo:
                    excedido = True
                    return {"type": "http.disconnect"}  # Django deja de leer y descarta lo recibido
            return mensaje

        async def enviar(mensaje):
            nonlocal respondio
            if excedido:
                return
            respondio = respondio or mensaje["type"] == "http.response.start"
            await send(mensaje)

        await self.app(scope, recibir, enviar)
        if excedido and not respondio:
            await _responder_413(send)


if settings.SERVIR_ESTATICOS:
    # Sin un servidor web delante que entregue /static/ (docs/30 A13): Django los sirve desde los finders.
    # Va por dentro de LimiteDeCuerpo, así el tope de cuerpo aplica también a esas rutas.
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

    django_application = ASGIStaticFilesHandler(django_application)

application = LimiteDeCuerpo(django_application)
