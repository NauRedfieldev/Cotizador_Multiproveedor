"""Pruebas de la configuración (settings.py) sin BD: cada caso importa settings en un proceso aparte.

Las variables que se pasan al proceso ganan sobre .env (read_env solo rellena las que faltan),
así que no se toca ni se lee el .env de nadie.
"""
import json
import os
import secrets
from pathlib import Path
import subprocess
import sys

from django.core.management.utils import get_random_secret_key
from django.test import SimpleTestCase

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
