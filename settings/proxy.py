"""Aviso en el log cuando hay un proxy delante que uvicorn no reconoce (pista 12 de la auditoría).

Si uvicorn no confía en el proxy (FORWARDED_ALLOW_IPS o --forwarded-allow-ips), deja pasar sus
encabezados sin usarlos: Django ve la IP del proxy en todas las peticiones (los límites por IP de
login y de recuperar contraseña se comparten entre todos) y cree que todo llega por http (con
USE_HTTPS la redirección a https no termina). Este middleware solo lo detecta y lo escribe una vez
por proceso; no cambia ninguna petición.

No detecta un proxy que no manda X-Forwarded-For. Si alguien manda el encabezado directo, puede
provocar el aviso, pero sale una sola vez y no cambia nada.
"""

import logging

from asgiref.sync import iscoroutinefunction
from django.conf import settings
from django.utils.decorators import sync_and_async_middleware

logger = logging.getLogger(__name__)
_avisados = set()


def _sin_puerto(entrada):
    """«1.2.3.4:5678» → «1.2.3.4»; «[::1]:80» → «::1»; una IPv6 sin corchetes se deja igual."""
    entrada = entrada.strip()
    if entrada.startswith("["):
        return entrada[1:entrada.find("]")]
    return entrada.split(":")[0] if entrada.count(":") == 1 else entrada


def _avisar(clave, mensaje, *args):
    if clave not in _avisados:
        _avisados.add(clave)
        logger.warning(mensaje, *args)


def revisar(request):
    if len(_avisados) == 2:
        return
    ip = request.META.get("REMOTE_ADDR", "")
    reenviado = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if reenviado and ip not in {_sin_puerto(entrada) for entrada in reenviado.split(",")}:
        _avisar("ip", "Hay un proxy delante que uvicorn no reconoce: las peticiones llegan con la IP %s y los "
                      "límites por IP (login, recuperar contraseña) se comparten entre todos. Configura "
                      "FORWARDED_ALLOW_IPS (o --forwarded-allow-ips) con la IP o red del proxy.", ip)
    if (settings.SECURE_SSL_REDIRECT and not request.is_secure()
            and request.META.get("HTTP_X_FORWARDED_PROTO", "").strip().lower() == "https"):
        _avisar("https", "El proxy avisa que la conexión es HTTPS, pero uvicorn no lo reconoce y Django la ve "
                         "como http: con USE_HTTPS=True la redirección a https no termina. Configura "
                         "FORWARDED_ALLOW_IPS (o --forwarded-allow-ips) con la IP o red del proxy (%s).", ip)


@sync_and_async_middleware
def avisar_proxy_sin_configurar(get_response):
    if iscoroutinefunction(get_response):
        async def middleware(request):
            revisar(request)
            return await get_response(request)
    else:
        def middleware(request):
            revisar(request)
            return get_response(request)
    return middleware
