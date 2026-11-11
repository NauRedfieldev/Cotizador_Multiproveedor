"""Pruebas de la configuración (settings.py) sin BD: cada caso importa settings en un proceso aparte.

Las variables que se pasan al proceso ganan sobre .env (read_env solo rellena las que faltan),
así que no se toca ni se lee el .env de nadie.
"""
import asyncio
import json
import os
import secrets
from pathlib import Path
import subprocess
import sys

from django.core.management.utils import get_random_secret_key
from django.test import RequestFactory, SimpleTestCase, override_settings

from settings import proxy

RAIZ = Path(__file__).resolve().parent.parent


def ejecutar(argumentos, **variables):
    entorno = {**os.environ, "USE_SQLITE": "1", **variables}   # SQLite: sin depender de credenciales de BD
    return subprocess.run([sys.executable, *argumentos], cwd=RAIZ, env=entorno,
                          capture_output=True, text=True, timeout=60)


def importar_settings(**variables):
    return ejecutar(["-c", "import settings.settings"], **variables)


def leer_settings(*nombres, **variables):
    """Valores de settings con estas variables; la llave es propia para no depender del .env."""
    codigo = ("import json, settings.settings as s; "
              f"print(json.dumps({{n: getattr(s, n) for n in {list(nombres)!r}}}))")
    resultado = ejecutar(["-c", codigo], **{"SECRET_KEY": get_random_secret_key(), **variables})
    assert resultado.returncode == 0, resultado.stderr
    return json.loads(resultado.stdout)


class SecretKeyTests(SimpleTestCase):
    def test_sin_llave_o_con_la_de_ejemplo_no_arranca(self):
        for valor in ("", "   ", "django-insecure-Example", "django-insecure-solo-desarrollo-local"):
            with self.subTest(valor=valor):
                resultado = importar_settings(SECRET_KEY=valor)
                self.assertNotEqual(resultado.returncode, 0)
                self.assertIn("ImproperlyConfigured", resultado.stderr)
                self.assertIn("Falta SECRET_KEY", resultado.stderr)

    def test_con_una_llave_propia_arranca(self):
        # Una llave que empieza con $ es válida (env() la tomaría por el nombre de otra variable).
        for valor in (get_random_secret_key(), "$" + get_random_secret_key()[1:], secrets.token_urlsafe(50)):
            with self.subTest(valor=valor[:1]):
                resultado = ejecutar(["-c", "import settings.settings as s, sys; sys.exit(s.SECRET_KEY != sys.argv[1])", valor],
                                     SECRET_KEY=valor)
                self.assertEqual(resultado.returncode, 0, resultado.stderr)


class ServidorTests(SimpleTestCase):
    """Pistas 8 (HTTPS) y 9 (ALLOWED_HOSTS) de la auditoría: se configuran desde .env."""

    HTTPS = ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT", "SECURE_HSTS_SECONDS")

    def test_allowed_hosts_sale_del_env(self):
        self.assertEqual(leer_settings("ALLOWED_HOSTS", ALLOWED_HOSTS="cotizador.example.com, 10.0.0.5,")["ALLOWED_HOSTS"],
                         ["cotizador.example.com", "10.0.0.5"])
        self.assertEqual(leer_settings("ALLOWED_HOSTS", ALLOWED_HOSTS="")["ALLOWED_HOSTS"], [])

    def test_https_apagado_en_local_y_encendido_en_el_servidor(self):
        self.assertEqual(leer_settings(*self.HTTPS, USE_HTTPS="False"),
                         dict.fromkeys(self.HTTPS[:3], False) | {"SECURE_HSTS_SECONDS": 0})
        self.assertEqual(leer_settings(*self.HTTPS, USE_HTTPS="True"),
                         dict.fromkeys(self.HTTPS[:3], True) | {"SECURE_HSTS_SECONDS": 3600})
        self.assertEqual(leer_settings("SECURE_HSTS_SECONDS", USE_HTTPS="True", SECURE_HSTS_SECONDS="31536000"),
                         {"SECURE_HSTS_SECONDS": 31536000})

    def test_check_deploy_sin_avisos_de_https_ni_hosts(self):
        resultado = ejecutar(["manage.py", "check", "--deploy"], SECRET_KEY=get_random_secret_key(), DEBUG="False",
                             USE_HTTPS="True", ALLOWED_HOSTS="cotizador.example.com", SECURE_HSTS_SECONDS="3600")
        salida = resultado.stdout + resultado.stderr
        self.assertIn("System check identified", salida)   # el check corrió de verdad
        for aviso in ("W004", "W008", "W009", "W012", "W016", "W018", "W020"):
            with self.subTest(aviso=aviso):
                self.assertNotIn(f"security.{aviso}", salida)


class DespliegueTests(SimpleTestCase):
    """docs/36: lo que la imagen de Docker necesita de settings.py."""

    def test_collectstatic_corre_como_en_el_dockerfile(self):
        # Mismas variables falsas que el Dockerfile, sin BD; --dry-run no escribe nada.
        resultado = ejecutar(["manage.py", "collectstatic", "--noinput", "--dry-run"], USE_SQLITE="False",
                             SECRET_KEY="solo-para-collectstatic", NAME_DB="x", USER_DB="x", PASSWORD_DB="x")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn(str(RAIZ / "staticfiles"), resultado.stdout)
        self.assertIn("css/custom.css", resultado.stdout)        # static/
        self.assertIn("admin/css/base.css", resultado.stdout)    # los del admin

    def test_log_a_consola_escribe_los_errores_500(self):
        # Sin LOG_A_CONSOLA, con DEBUG=False la traza de un 500 no sale en ningún lado (como antes).
        codigo = ("import django, logging; django.setup(); "
                  "logging.getLogger('django.request').error('Internal Server Error: /prueba-log/')")
        for valor, se_escribe in (("True", True), ("False", False)):
            with self.subTest(LOG_A_CONSOLA=valor):
                resultado = ejecutar(["-c", codigo], DJANGO_SETTINGS_MODULE="settings.settings",
                                     SECRET_KEY=get_random_secret_key(), DEBUG="False", LOG_A_CONSOLA=valor)
                self.assertEqual(resultado.returncode, 0, resultado.stderr)
                self.assertEqual("/prueba-log/" in resultado.stderr, se_escribe, resultado.stderr)


class AvisoDeProxyTests(SimpleTestCase):
    """Pista 12 de la auditoría: un proxy que uvicorn no reconoce se avisa en el log, una sola vez."""

    def setUp(self):
        proxy._avisados.clear()
        self.addCleanup(proxy._avisados.clear)

    def peticion(self, ip="172.17.0.1", seguro=False, **encabezados):
        return RequestFactory().get("/login/", REMOTE_ADDR=ip, secure=seguro, headers=encabezados)

    def test_proxy_no_reconocido_avisa_con_la_ip_que_ve(self):
        with self.assertLogs("settings.proxy", "WARNING") as registro:
            proxy.revisar(self.peticion(**{"X-Forwarded-For": "201.150.10.20"}))
        self.assertIn("172.17.0.1", registro.output[0])
        self.assertIn("FORWARDED_ALLOW_IPS", registro.output[0])
        self.assertNotIn("201.150.10.20", registro.output[0])  # la IP de la persona no va al log

    def test_proxy_reconocido_o_sin_proxy_no_avisa(self):
        casos = [
            self.peticion(ip="201.150.10.20", **{"X-Forwarded-For": "10.1.1.1, 201.150.10.20"}),
            self.peticion(ip="201.150.10.20", **{"X-Forwarded-For": "201.150.10.20:51234"}),
            self.peticion(ip="2001:db8::5", **{"X-Forwarded-For": "[2001:db8::5]:443"}),
            self.peticion(ip="201.150.10.20"),
        ]
        with self.assertNoLogs("settings.proxy"):
            for peticion in casos:
                proxy.revisar(peticion)

    def test_avisa_una_sola_vez(self):
        with self.assertLogs("settings.proxy", "WARNING") as registro:
            for _ in range(3):
                proxy.revisar(self.peticion(**{"X-Forwarded-For": "201.150.10.20"}))
        self.assertEqual(len(registro.output), 1)

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_https_no_reconocido_avisa(self):
        with self.assertLogs("settings.proxy", "WARNING") as registro:
            proxy.revisar(self.peticion(**{"X-Forwarded-Proto": "https"}))
        self.assertIn("redirección a https no termina", registro.output[0])
        proxy._avisados.clear()
        with self.assertNoLogs("settings.proxy"):
            proxy.revisar(self.peticion(seguro=True, **{"X-Forwarded-Proto": "https"}))
        with override_settings(SECURE_SSL_REDIRECT=False), self.assertNoLogs("settings.proxy"):
            proxy.revisar(self.peticion(**{"X-Forwarded-Proto": "https"}))

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_el_middleware_avisa_antes_de_redirigir(self):
        with self.assertLogs("settings.proxy", "WARNING"):
            respuesta = self.client.get("/login/", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(respuesta.status_code, 301)
        self.assertEqual(respuesta["Location"], "https://testserver/login/")

    @override_settings(SECURE_SSL_REDIRECT=True)
    async def test_el_middleware_avisa_tambien_por_asgi(self):
        with self.assertLogs("settings.proxy", "WARNING"):
            respuesta = await self.async_client.get("/login/", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(respuesta.status_code, 301)


async def llamar_asgi(metodo, ruta, cuerpos=(b"",), encabezados=()):
    """Llama a settings.asgi.application como lo haría uvicorn. Devuelve (estado, mensajes del cuerpo leídos)."""
    from settings import asgi
    mensajes = [{"type": "http.request", "body": cuerpo, "more_body": i < len(cuerpos) - 1}
                for i, cuerpo in enumerate(cuerpos)]
    leidos = 0

    async def receive():
        nonlocal leidos
        if leidos < len(mensajes):
            leidos += 1
            return mensajes[leidos - 1]
        await asyncio.Event().wait()  # el cliente sigue conectado

    enviados = []

    async def send(mensaje):
        enviados.append(mensaje)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": metodo, "scheme": "http",
             "path": ruta, "raw_path": ruta.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"testserver"), *encabezados], "client": ("127.0.0.1", 5000),
             "server": ("testserver", 80)}
    await asyncio.wait_for(asgi.application(scope, receive, send), 10)
    return enviados[0]["status"], leidos


@override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1000)
class LimiteDeCuerpoTests(SimpleTestCase):
    """Pista 15 de la auditoría: un cuerpo de más del tope se corta con 413 antes de que Django lo lea entero."""

    async def test_content_length_grande_se_corta_sin_leer(self):
        for largo in (b"1001", b"999999999999", b"abc"):
            with self.subTest(largo=largo):
                estado, leidos = await llamar_asgi("POST", "/no-existe/", [b"x" * 10], [(b"content-length", largo)])
                self.assertEqual((estado, leidos), (413, 0))

    async def test_sin_content_length_se_corta_al_pasar_el_tope(self):
        estado, leidos = await llamar_asgi("POST", "/no-existe/", [b"x" * 100] * 50)
        self.assertEqual(estado, 413)
        self.assertLessEqual(leidos, 11)  # dejó de leer en cuanto pasó de 1000 bytes

    async def test_lo_normal_pasa_a_django(self):
        self.assertEqual(await llamar_asgi("GET", "/login/"), (200, 1))
        estado, _ = await llamar_asgi("POST", "/login/", [b"a" * 1000], [(b"content-length", b"1000")])
        self.assertEqual(estado, 403)  # llegó a Django: falta el token CSRF
