"""Backend SMTP que no se queda esperando una dirección que no responde.

Un servidor como smtp.gmail.com tiene varias direcciones (IPv6 e IPv4). Python las prueba en orden y le da a
cada una la espera completa del envío (20 s). Si la red anuncia IPv6 pero no lo enruta, cada envío esperaba
esos 20 s antes de pasar a IPv4, que conecta al instante. Aquí cada dirección tiene ESPERA_CONEXION para
conectar; ya conectados, el envío conserva su espera completa. Además se recuerda qué tipo de dirección
funcionó, así que solo el primer envío tras arrancar el servidor paga esa espera.

La ruta «apps.quotes.smtp.EmailBackend» termina en «smtp.EmailBackend» a propósito: views.py y usuarios.py
reconocen así el correo real frente al de consola.
"""
import smtplib
import socket

from django.core.mail.backends.smtp import EmailBackend as _EmailBackendDjango

ESPERA_CONEXION = 3  # segundos para abrir la conexión con cada dirección del servidor

# Tipo de dirección (IPv4/IPv6) que conectó la última vez con cada servidor: se prueba primero en el siguiente
# envío, para no volver a perder ESPERA_CONEXION en la que no responde. Vive en memoria del proceso.
_familia_que_funciono = {}


def conectar(host, port, timeout, source_address=None):
    """Como socket.create_connection, pero con una espera corta por dirección."""
    espera = ESPERA_CONEXION if timeout is None else min(ESPERA_CONEXION, timeout)
    preferida = _familia_que_funciono.get((host, port))
    # sorted es estable: dentro de cada tipo se conserva el orden que dio el DNS.
    direcciones = sorted(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM), key=lambda d: d[0] != preferida)
    ultimo_error = None
    for familia, tipo, protocolo, _, direccion in direcciones:
        sock = socket.socket(familia, tipo, protocolo)
        try:
            sock.settimeout(espera)
            if source_address:
                sock.bind(source_address)
            sock.connect(direccion)
        except OSError as exc:
            sock.close()
            ultimo_error = exc
            continue
        sock.settimeout(timeout)
        _familia_que_funciono[(host, port)] = familia
        return sock
    raise ultimo_error or OSError(f"Sin direcciones para {host}.")


class SMTPRapido(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        return conectar(host, port, timeout, self.source_address)


class SMTPSSLRapido(smtplib.SMTP_SSL, SMTPRapido):
    """SMTP_SSL cifra el socket que le entrega SMTPRapido (orden de herencia: SMTP_SSL → SMTPRapido → SMTP)."""


class EmailBackend(_EmailBackendDjango):
    @property
    def connection_class(self):
        return SMTPSSLRapido if self.use_ssl else SMTPRapido
