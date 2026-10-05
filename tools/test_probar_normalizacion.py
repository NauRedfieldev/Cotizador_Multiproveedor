"""Pruebas de terminal sin Django, PostgreSQL ni dependencias externas."""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

SCRIPT = Path(__file__).resolve().with_name("probar_normalizacion.py")


class TerminalTests(unittest.TestCase):
    def ejecutar(self, entrada, *argumentos):
        entorno = os.environ.copy()
        for variable in ("DJANGO_SETTINGS_MODULE", "SECRET_KEY", "NAME_DB", "USER_DB", "PASSWORD_DB"):
            entorno.pop(variable, None)
        entorno["PYTHONIOENCODING"] = "utf-8"
        # El script debe encontrar el catálogo sin depender del directorio actual.
        with TemporaryDirectory() as carpeta:
            return subprocess.run(
                [sys.executable, str(SCRIPT), *argumentos],
                input=entrada, capture_output=True, text=True, encoding="utf-8",
                env=entorno, cwd=carpeta, timeout=10,
            )

    def test_texto_predeterminado_y_varias_entradas(self):
        resultado = self.ejecutar("Cámara IP 2.0 / POE+\nSW@24P#RACK\nsalir\n")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("Normalizado: CAMARA IP 2.0 / POE+", resultado.stdout)
        self.assertIn("Normalizado: SW 24P RACK", resultado.stdout)

    def test_modo_clave_explicito_y_advertencia(self):
        resultado = self.ejecutar("C9200L-24P-4G-E\nsalir\n", "--modo", "clave")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("Normalizado: C9200L24P4GE", resultado.stdout)
        self.assertIn("AVISO:", resultado.stdout)

    def test_preserva_poe_y_no_expande_sinonimos(self):
        resultado = self.ejecutar("POE\nPOE+\nSW 24P\nsalir\n", "--modo", "texto")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("Normalizado: POE\n", resultado.stdout)
        self.assertIn("Normalizado: POE+\n", resultado.stdout)
        self.assertIn("Normalizado: SW 24P\n", resultado.stdout)

    def test_entrada_vacia_y_fin_de_entrada(self):
        resultado = self.ejecutar("  \n")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("Entrada vacía:", resultado.stdout)
        self.assertIn("Fin.", resultado.stdout)

    def test_salir_no_se_normaliza(self):
        resultado = self.ejecutar("  SALIR  \n")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertNotIn("Normalizado:", resultado.stdout)

    def test_ayuda_y_modo_invalido(self):
        ayuda = self.ejecutar("", "--help")
        self.assertEqual(ayuda.returncode, 0, ayuda.stderr)
        self.assertIn("--modo", ayuda.stdout)
        invalido = self.ejecutar("", "--modo", "otro")
        self.assertEqual(invalido.returncode, 2)


if __name__ == "__main__":
    unittest.main()
