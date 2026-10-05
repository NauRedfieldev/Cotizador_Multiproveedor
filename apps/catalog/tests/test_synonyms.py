"""Sinónimos sintéticos; no se precargan equivalencias en producción."""

from threading import get_ident
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase

from apps.catalog.models import SinonimoRed
from apps.catalog.synonyms import aexpandir, expandir, invalidar_cache


class CacheUnitTests(SimpleTestCase):
    def setUp(self):
        invalidar_cache()
        self.addCleanup(invalidar_cache)

    def test_ttl_e_invalidacion_explicita(self):
        with patch("apps.catalog.synonyms.monotonic") as reloj, patch(
            "apps.catalog.synonyms._leer_sinonimos", return_value={"SW": "SWITCH"}
        ) as leer:
            reloj.return_value = 100
            self.assertEqual(expandir("SW 24P"), "SWITCH 24P")
            reloj.return_value = 159
            self.assertEqual(expandir("SW"), "SWITCH")
            self.assertEqual(leer.call_count, 1)
            reloj.return_value = 160
            expandir("SW")
            self.assertEqual(leer.call_count, 2)
            invalidar_cache()
            expandir("SW")
            self.assertEqual(leer.call_count, 3)

    def test_no_sustituye_parcialmente_ni_encadena(self):
        with patch("apps.catalog.synonyms._leer_sinonimos", return_value={"SW": "SWITCH", "SWITCH": "OTRO"}):
            self.assertEqual(expandir("SW SW24P SW+ SWITCH"), "SWITCH SW24P SW+ OTRO")

    def test_texto_vacio_no_carga_diccionario(self):
        with patch("apps.catalog.synonyms._leer_sinonimos") as leer:
            self.assertEqual(expandir("  "), "")
            leer.assert_not_called()

    async def test_via_async_carga_fuera_del_event_loop(self):
        hilo_event_loop = get_ident()

        def leer():
            self.assertNotEqual(get_ident(), hilo_event_loop)
            return {"SW": "SWITCH"}

        with patch("apps.catalog.synonyms._leer_sinonimos", side_effect=leer):
            self.assertEqual(await aexpandir("SW 24P"), "SWITCH 24P")


class SynonymTests(TransactionTestCase):
    def setUp(self):
        invalidar_cache()
        self.addCleanup(invalidar_cache)
        self.sinonimo = SinonimoRed.objects.create(abreviatura="sw", expansion="switch")

    def test_expansion_desde_bd_y_cache(self):
        with self.assertNumQueries(1):
            self.assertEqual(expandir("SW 24P"), "SWITCH 24P")
        with self.assertNumQueries(0):
            self.assertEqual(expandir("SW POE+ CAT6A 4X10G"), "SWITCH POE+ CAT6A 4X10G")

    def test_guardado_invalida_cache(self):
        expandir("SW")
        self.sinonimo.expansion = "SWITCH ADMINISTRABLE"
        self.sinonimo.save()
        with self.assertNumQueries(1):
            self.assertEqual(expandir("SW"), "SWITCH ADMINISTRABLE")

    def test_borrado_individual_invalida_cache(self):
        expandir("SW")
        self.sinonimo.delete()
        self.assertEqual(expandir("SW"), "SW")

    def test_borrado_queryset_invalida_cache(self):
        expandir("SW")
        SinonimoRed.objects.all().delete()
        self.assertEqual(expandir("SW"), "SW")

    def test_creacion_invalida_cache_vacia(self):
        self.sinonimo.delete()
        self.assertEqual(expandir("SW"), "SW")
        SinonimoRed.objects.create(abreviatura="SW", expansion="SWITCH")
        self.assertEqual(expandir("SW"), "SWITCH")

    def test_expansion_multipalabra(self):
        SinonimoRed.objects.create(abreviatura="TEST", expansion="DOS PALABRAS")
        self.assertEqual(expandir("SW TEST"), "SWITCH DOS PALABRAS")

    def test_rollback_no_publica_datos_transitorios(self):
        self.assertEqual(expandir("SW"), "SWITCH")
        with transaction.atomic():
            self.sinonimo.expansion = "TEMPORAL"
            self.sinonimo.save()
            self.assertEqual(expandir("SW"), "TEMPORAL")
            transaction.set_rollback(True)
        self.assertEqual(expandir("SW"), "SWITCH")

    def test_commit_vuelve_a_invalidar(self):
        with transaction.atomic():
            self.sinonimo.expansion = "SWITCH NUEVO"
            self.sinonimo.save()
            # Simula otra lectura que rellena la caché con la versión pre-commit.
            with patch("apps.catalog.synonyms.connection") as conexion, patch(
                "apps.catalog.synonyms._leer_sinonimos", return_value={"SW": "SWITCH"}
            ):
                conexion.in_atomic_block = False
                self.assertEqual(expandir("SW"), "SWITCH")
        self.assertEqual(expandir("SW"), "SWITCH NUEVO")

    async def test_expansion_async_con_cache_fria(self):
        self.assertEqual(await aexpandir("SW 24P"), "SWITCH 24P")
        self.assertEqual(await aexpandir("SW POE+"), "SWITCH POE+")
