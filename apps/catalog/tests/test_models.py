from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from apps.catalog.models import SinonimoRed
from apps.catalog.synonyms import invalidar_cache


class SinonimoValidationTests(SimpleTestCase):
    def test_validacion_normaliza_sin_consultar_bd(self):
        sinonimo = SinonimoRed(abreviatura=" sfp+ ", expansion=" módulo óptico ")
        sinonimo.full_clean(validate_unique=False)
        self.assertEqual(sinonimo.abreviatura, "SFP+")
        self.assertEqual(sinonimo.expansion, "MODULO OPTICO")

    def test_validacion_rechaza_datos_invalidos_sin_consultar_bd(self):
        for abreviatura, expansion in [
            ("@", "SWITCH"), ("SW RACK", "SWITCH"), ("SW", "@"),
            ("A" * 41, "SWITCH"), ("SW", "A" * 121),
        ]:
            with self.subTest(abreviatura=abreviatura, expansion=expansion):
                with self.assertRaises(ValidationError):
                    SinonimoRed(abreviatura=abreviatura, expansion=expansion).full_clean(
                        validate_unique=False,
                    )


class SinonimoRedTests(TestCase):
    def setUp(self):
        invalidar_cache()
        self.addCleanup(invalidar_cache)

    def test_guardado_normalizado(self):
        sinonimo = SinonimoRed.objects.create(abreviatura="  sw  ", expansion="swítch")
        sinonimo.refresh_from_db()
        self.assertEqual(sinonimo.abreviatura, "SW")
        self.assertEqual(sinonimo.expansion, "SWITCH")

    def test_expansion_de_varias_palabras_y_signos(self):
        sinonimo = SinonimoRed.objects.create(abreviatura="sfp+", expansion=" módulo sfp+ ")
        self.assertEqual(sinonimo.abreviatura, "SFP+")
        self.assertEqual(sinonimo.expansion, "MODULO SFP+")

    def test_unicidad_tras_normalizar(self):
        SinonimoRed.objects.create(abreviatura="SW", expansion="SWITCH")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SinonimoRed.objects.create(abreviatura=" sw ", expansion="OTRO")

    def test_full_clean_normaliza_antes_de_validar_unicidad(self):
        SinonimoRed.objects.create(abreviatura="SW", expansion="SWITCH")
        with self.assertRaises(ValidationError) as error:
            SinonimoRed(abreviatura="sw", expansion="switch").full_clean()
        self.assertIn("abreviatura", error.exception.message_dict)

    def test_rechaza_abreviatura_vacia_o_de_varios_tokens(self):
        for abreviatura in ["", "   ", "@", "SW RACK"]:
            with self.subTest(abreviatura=abreviatura):
                with self.assertRaises(ValidationError):
                    SinonimoRed.objects.create(abreviatura=abreviatura, expansion="SWITCH")

    def test_rechaza_expansion_vacia_y_longitudes_excesivas(self):
        for abreviatura, expansion in [("SW", "@"), ("A" * 41, "SWITCH"), ("SW", "A" * 121)]:
            with self.subTest(abreviatura=abreviatura, expansion=expansion):
                with self.assertRaises(ValidationError):
                    SinonimoRed.objects.create(abreviatura=abreviatura, expansion=expansion)

    def test_registrado_en_admin(self):
        self.assertIn(SinonimoRed, admin.site._registry)
