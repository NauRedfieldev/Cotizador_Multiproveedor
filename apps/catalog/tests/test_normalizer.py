"""Ejemplos sintéticos; no representan catálogos reales de proveedores."""

from django.test import SimpleTestCase

from apps.catalog.normalizer import normalizar, normalizar_clave


class NormalizerTests(SimpleTestCase):
    # SimpleTestCase prohíbe accesos a BD: las funciones deben ser puras.
    def test_normalizar_e_idempotencia(self):
        casos = [
            ("Switch administrável", "SWITCH ADMINISTRAVEL"),
            ("  SW   24P  ", "SW 24P"),
            ("Cámara IP 2.0", "CAMARA IP 2.0"),
            ("POE+", "POE+"),
            ("POE", "POE"),
            ("1/2 U", "1/2 U"),
            ("SW@24P#RACK", "SW 24P RACK"),
            ("  Cámara IP ángulo 2.0 / POE+  ", "CAMARA IP ANGULO 2.0 / POE+"),
            ("ＳＷ\t24P\nRACK", "SW 24P RACK"),
            ("a\u0301ngulo — rack-1", "ANGULO RACK-1"),
            ("", ""),
            ("   @#   ", ""),
        ]
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                self.assertEqual(normalizar(entrada), esperado)
                self.assertEqual(normalizar(esperado), esperado)

    def test_claves_y_separadores(self):
        casos = [
            ("C9200L-24P-4G-E", "C9200L24P4GE"),
            ("c9200l 24p 4g e", "C9200L24P4GE"),
            ("C9200L24P4GE", "C9200L24P4GE"),
            ("ABC/123.4", "ABC1234"),
            (" á-Ｂ_12+ ", "12"),  # Las claves no aplican NFKD.
            ("", ""),
        ]
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                self.assertEqual(normalizar_clave(entrada), esperado)
                self.assertEqual(normalizar_clave(esperado), esperado)

    def test_preserva_variantes_tecnicas(self):
        for a, b in [("POE", "POE+"), ("4X1G", "4X10G"), ("CAT6", "CAT6A"), ("1GB", "1GBPS")]:
            with self.subTest(a=a, b=b):
                self.assertNotEqual(normalizar(a), normalizar(b))

    def test_none_no_se_convierte_en_cadena_vacia(self):
        for funcion in (normalizar, normalizar_clave):
            with self.subTest(funcion=funcion.__name__):
                with self.assertRaises((TypeError, AttributeError)):
                    funcion(None)
