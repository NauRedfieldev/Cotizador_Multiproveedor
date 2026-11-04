"""Pruebas de la configuración (settings.py) sin BD: cada caso importa settings en un proceso aparte.

Las variables que se pasan al proceso ganan sobre .env (read_env solo rellena las que faltan),
así que no se toca ni se lee el .env de nadie.
"""
import os
from pathlib import Path
import subprocess
import sys

from django.core.management.utils import get_random_secret_key
from django.test import SimpleTestCase

RAIZ = Path(__file__).resolve().parent.parent


def importar_settings(**variables):
    entorno = {**os.environ, "USE_SQLITE": "1", **variables}   # SQLite: sin depender de credenciales de BD
    return subprocess.run([sys.executable, "-c", "import settings.settings"], cwd=RAIZ, env=entorno,
                          capture_output=True, text=True, timeout=30)


class SecretKeyTests(SimpleTestCase):
    def test_sin_llave_o_con_la_de_ejemplo_no_arranca(self):
        for valor in ("", "   ", "django-insecure-Example", "django-insecure-solo-desarrollo-local"):
            with self.subTest(valor=valor):
                resultado = importar_settings(SECRET_KEY=valor)
                self.assertNotEqual(resultado.returncode, 0)
                self.assertIn("ImproperlyConfigured", resultado.stderr)
                self.assertIn("Falta SECRET_KEY", resultado.stderr)

    def test_con_una_llave_propia_arranca(self):
        resultado = importar_settings(SECRET_KEY=get_random_secret_key())
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
