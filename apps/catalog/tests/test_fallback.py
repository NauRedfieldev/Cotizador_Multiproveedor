"""Fixtures sintéticos de M5c; no hay calibración ni llamadas a APIs reales."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from rapidfuzz import fuzz

from apps.catalog.constants import EstadoMatch, MetodoMatch
from apps.catalog.matching import (
    IdentificadoresConfiables, aemparejar_con_fallback, emparejar_con_fallback,
    resolver_con_fallback,
)
from apps.catalog.models import Category, ProductoCanonico, SinonimoRed
from apps.catalog.synonyms import invalidar_cache, obtener_sinonimos
from apps.catalog.tests.test_matching import candidato, oferta


class FallbackUnitTests(SimpleTestCase):
    def resolver(self, productos=None, **kwargs):
        productos = [candidato(descripcion="SWITCH 24P POE+ 4X1G")] if productos is None else productos
        datos = dict(categoria_id=1, sinonimos={}, confianza=IdentificadoresConfiables())
        datos.update(kwargs)
        return resolver_con_fallback(oferta(name="SWITCH 24P POE+ 4X1G"), productos, **datos)

    def test_score_100_solo_sugiere_no_asocia(self):
        resultado = self.resolver()
        self.assertEqual(resultado.estado, EstadoMatch.REVISION_MANUAL)
        self.assertEqual(resultado.metodo, MetodoMatch.FUZZY)
        self.assertEqual(resultado.score, 100)
        self.assertEqual(resultado.motivo, "FUZZY_UMBRAL_ALTO_SIN_CALIBRAR")
        self.assertIsNone(resultado.canonico)

    def test_sinonimos_aplicados_a_ambos_textos(self):
        resultado = resolver_con_fallback(
            oferta(name="SW 24P POE+"), [candidato(descripcion="SWITCH 24P POE+")],
            categoria_id=1, sinonimos={"SW": "SWITCH"},
        )
        self.assertEqual(resultado.score, 100)

    def test_bloqueo_marca_categoria(self):
        for producto in [candidato(marca="Otra marca"), candidato(categoria_id=2)]:
            with self.subTest(pk=producto.pk, marca=producto.marca, categoria=producto.categoria_id):
                self.assertEqual(self.resolver([producto]).motivo, "FALLBACK_SIN_CANDIDATOS")

    def test_marca_ausente(self):
        resultado = resolver_con_fallback(oferta(brand=None), [candidato()], categoria_id=1, sinonimos={})
        self.assertEqual(resultado.motivo, "FALLBACK_SIN_MARCA")

    def test_categoria_ausente_o_invalida(self):
        self.assertEqual(self.resolver(categoria_id=None).motivo, "FALLBACK_SIN_CATEGORIA")
        for valor in [True, 0, -1, "1"]:
            with self.subTest(valor=valor):
                self.assertEqual(self.resolver(categoria_id=valor).motivo, "FALLBACK_CATEGORIA_INVALIDA")

    def test_texto_sin_informacion(self):
        for texto in ["", "   ", "@#", "+ / . -"]:
            resultado = resolver_con_fallback(oferta(name=texto), [candidato()], categoria_id=1, sinonimos={})
            self.assertEqual(resultado.motivo, "FALLBACK_TEXTO_SIN_INFORMACION")

    def test_sin_candidatos(self):
        self.assertEqual(self.resolver([]).motivo, "FALLBACK_SIN_CANDIDATOS")

    def test_guardas_anulan_score_alto(self):
        for descripcion in ["SWITCH 48P POE+ 4X1G", "SWITCH 24P POE 4X1G", "SWITCH 24P POE+ 4X10G"]:
            with self.subTest(descripcion=descripcion), patch("apps.catalog.matching.fuzz.token_set_ratio", return_value=99):
                self.assertEqual(self.resolver([candidato(descripcion=descripcion)]).motivo,
                                 "FALLBACK_SIN_CANDIDATOS_COMPATIBLES")

    def test_revisa_todos_los_candidatos_no_solo_el_mejor(self):
        productos = [candidato(1, descripcion="SWITCH 24P POE 4X1G"),
                     candidato(2, descripcion="SWITCH 24P POE+ 4X1G ADMINISTRABLE")]
        with patch("apps.catalog.matching.fuzz.token_set_ratio", side_effect=[99, 80]):
            resultado = self.resolver(productos)
        self.assertEqual(resultado.candidatos, (2,))
        self.assertEqual(resultado.score, 80)

    def test_guardas_no_dependen_de_expansion(self):
        resultado = self.resolver([candidato(descripcion="SWITCH 24P POE 4X1G")],
                                 sinonimos={"POE": "ALIMENTACION", "POE+": "ALIMENTACION"})
        self.assertEqual(resultado.estado, EstadoMatch.SIN_MATCH)

    def test_texto_tecnicamente_ambiguo_no_se_sugiere(self):
        resultado = resolver_con_fallback(oferta(name="SWITCH POE POE+"),
                                         [candidato(descripcion="SWITCH POE+")], categoria_id=1, sinonimos={})
        self.assertEqual(resultado.motivo, "FALLBACK_SIN_CANDIDATOS_COMPATIBLES")

    def test_limites_sin_redondear_hacia_arriba(self):
        for score, estado, mostrado, motivo in [
            (69.9999, EstadoMatch.SIN_MATCH, 69, "FUZZY_BAJO_UMBRAL"),
            (70.0, EstadoMatch.REVISION_MANUAL, 70, "SUGERENCIAS_FUZZY"),
            (89.9999, EstadoMatch.REVISION_MANUAL, 89, "SUGERENCIAS_FUZZY"),
            (90.0, EstadoMatch.REVISION_MANUAL, 90, "FUZZY_UMBRAL_ALTO_SIN_CALIBRAR"),
        ]:
            with self.subTest(score=score), patch("apps.catalog.matching.fuzz.token_set_ratio", return_value=score):
                resultado = self.resolver()
                self.assertEqual((resultado.estado, resultado.score, resultado.motivo), (estado, mostrado, motivo))

    def test_empate_y_duplicados_no_eligen_un_producto(self):
        productos = [candidato(2, descripcion="SWITCH 24P POE+ 4X1G"),
                     candidato(1, descripcion="SWITCH 24P POE+ 4X1G")]
        resultado = self.resolver(productos)
        self.assertEqual(resultado.motivo, "FUZZY_EMPATE")
        self.assertEqual(resultado.candidatos, (1, 2))
        self.assertIsNone(resultado.canonico)

    def test_puntuaciones_cercanas_son_sugerencias_no_una_seleccion(self):
        with patch("apps.catalog.matching.fuzz.token_set_ratio", side_effect=[90, 89]):
            resultado = self.resolver([candidato(1, descripcion="SWITCH"), candidato(2, descripcion="SWITCH")])
        self.assertEqual(resultado.candidatos, (1, 2))
        self.assertIsNone(resultado.canonico)

    def test_m5b_auto_match_se_conserva_sin_puntuar(self):
        with patch("apps.catalog.matching.fuzz.token_set_ratio", side_effect=AssertionError("No usar fuzzy")):
            resultado = self.resolver(confianza=IdentificadoresConfiables(mpn_confirmado=True), categoria_id=None)
        self.assertEqual(resultado.estado, EstadoMatch.AUTO_MATCH)
        self.assertEqual(resultado.metodo, MetodoMatch.DETERMINISTICO_MPN)

    def test_no_anula_revision_colisiones_ni_conflictos_m5b(self):
        casos = [
            [candidato(1), candidato(2)],
            [candidato(mpn="MPN1")],
            [candidato(modelo="OTRO")],
        ]
        for productos in casos:
            with self.subTest(productos=[p.pk for p in productos]), patch(
                "apps.catalog.matching.fuzz.token_set_ratio", side_effect=AssertionError("No usar fuzzy")
            ):
                resultado = self.resolver(productos, confianza=IdentificadoresConfiables(mpn_confirmado=True, modelo_confirmado=True))
                self.assertEqual(resultado.estado, EstadoMatch.REVISION_MANUAL)
                self.assertNotEqual(resultado.metodo, MetodoMatch.FUZZY)

    def test_no_oculta_ambiguedad_m5b_por_categoria(self):
        resultado = self.resolver([candidato(1), candidato(2, categoria_id=2)],
                                 confianza=IdentificadoresConfiables(mpn_confirmado=True))
        self.assertEqual(resultado.motivo, "MULTIPLES_CANDIDATOS")

    def test_identificador_confiable_contradictorio_descarta_fuzzy(self):
        resultado = self.resolver([candidato(mpn="OTRO", descripcion="SWITCH 24P POE+ 4X1G")],
                                 confianza=IdentificadoresConfiables(mpn_confirmado=True))
        self.assertEqual(resultado.motivo, "FALLBACK_SIN_CANDIDATOS_COMPATIBLES")

    def test_identificador_ausente_no_es_contradiccion(self):
        resultado = self.resolver([candidato(mpn="", descripcion="SWITCH 24P POE+ 4X1G")],
                                 confianza=IdentificadoresConfiables(mpn_confirmado=True))
        self.assertEqual(resultado.estado, EstadoMatch.REVISION_MANUAL)
        self.assertEqual(resultado.metodo, MetodoMatch.FUZZY)

    def test_conflictos_de_modelo_y_sku_confirmados(self):
        for campo, confianza in [
            ("modelo", IdentificadoresConfiables(modelo_confirmado=True)),
            ("sku_fabricante", IdentificadoresConfiables(sku_fabricante_confirmado="FAB-1")),
        ]:
            with self.subTest(campo=campo):
                resultado = self.resolver([
                    candidato(descripcion="SWITCH 24P POE+ 4X1G", **{campo: "OTRO"}),
                ], confianza=confianza)
                self.assertEqual(resultado.motivo, "FALLBACK_SIN_CANDIDATOS_COMPATIBLES")

    def test_identificadores_no_confirmados_no_bloquean_sugerencias(self):
        resultado = self.resolver([
            candidato(mpn="OTRO", modelo="OTRO", sku_fabricante="OTRO",
                      descripcion="SWITCH 24P POE+ 4X1G"),
        ])
        self.assertEqual(resultado.estado, EstadoMatch.REVISION_MANUAL)

    def test_guardas_de_licencia_y_rack_en_flujo_completo(self):
        for a, b in [("LAN BASE", "LAN LITE"), ("1U", "2U")]:
            with self.subTest(a=a, b=b):
                resultado = resolver_con_fallback(
                    oferta(name=f"SWITCH 24P {a}"), [candidato(descripcion=f"SWITCH 24P {b}")],
                    categoria_id=1, sinonimos={},
                )
                self.assertEqual(resultado.motivo, "FALLBACK_SIN_CANDIDATOS_COMPATIBLES")

    def test_score_real_bajo_y_candidato_sin_descripcion_util(self):
        resultado = self.resolver([candidato(descripcion="ZZZZZ")])
        self.assertEqual(resultado.motivo, "FUZZY_BAJO_UMBRAL")
        for texto in ["", "+ / ."]:
            producto = candidato()
            producto.descripcion = texto
            self.assertEqual(self.resolver([producto]).motivo, "FALLBACK_SIN_CANDIDATOS_COMPATIBLES")

    def test_orden_de_entrada_no_cambia_resultado(self):
        productos = [candidato(2, descripcion="SWITCH 24P POE+ 4X1G"),
                     candidato(1, descripcion="SWITCH 24P POE+ 4X1G ADMINISTRABLE")]
        self.assertEqual(self.resolver(productos), self.resolver(reversed(productos)))

    def test_no_lee_raw_ni_datos_comerciales_o_codigos_internos(self):
        class PayloadProhibido(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("No leer JSON crudo")

            def __getitem__(self, key):
                raise AssertionError("No leer JSON crudo")

            def __iter__(self):
                raise AssertionError("No leer JSON crudo")

        a = oferta(name="SWITCH 24P POE+", raw=PayloadProhibido())
        b = replace(a, provider_code="exel", external_id="SWITCH-24P", price=Decimal("1"),
                    currency="USD", stock=999)
        productos = [candidato(descripcion="SWITCH 24P POE+")]
        self.assertEqual(
            resolver_con_fallback(a, productos, categoria_id=1, sinonimos={}),
            resolver_con_fallback(b, productos, categoria_id=1, sinonimos={}),
        )

    def test_no_promueve_colision_de_marca(self):
        resultado = resolver_con_fallback(oferta(brand="Marca@X", name="SWITCH"),
                                         [candidato(descripcion="SWITCH")], categoria_id=1, sinonimos={})
        self.assertEqual(resultado.estado, EstadoMatch.SIN_MATCH)

    def test_repeticion_de_instancia_y_candidato_sin_id(self):
        producto = candidato(descripcion="SWITCH 24P POE+ 4X1G")
        self.assertNotEqual(self.resolver([producto, producto]).motivo, "FUZZY_EMPATE")
        producto.pk = None
        with self.assertRaises(ValueError):
            self.resolver([producto])


class FallbackDatabaseTests(TestCase):
    def setUp(self):
        invalidar_cache()
        self.addCleanup(invalidar_cache)
        self.categoria = Category.objects.create(name="Redes sintéticas")
        self.producto = ProductoCanonico.objects.create(marca="Marca X", descripcion="SWITCH 24P POE+",
                                                       categoria=self.categoria)
        SinonimoRed.objects.create(abreviatura="SW", expansion="SWITCH")

    def test_flujo_completo_con_sinonimos_sin_escrituras_ni_red(self):
        with patch("socket.socket", side_effect=AssertionError("No consultar red")), CaptureQueriesContext(connection) as consultas:
            resultado = emparejar_con_fallback(oferta(name="SW 24P POE+"), categoria_id=self.categoria.pk)
        self.assertEqual(resultado.score, 100)
        self.assertEqual(resultado.estado, EstadoMatch.REVISION_MANUAL)
        self.assertEqual(resultado.candidatos, (self.producto.pk,))
        self.assertEqual(len(consultas), 3)
        self.assertTrue(all(c["sql"].lstrip().startswith("SELECT") for c in consultas))
        self.assertEqual(ProductoCanonico.objects.count(), 1)

    def test_categoria_inexistente_o_inactiva(self):
        self.assertEqual(emparejar_con_fallback(oferta(), categoria_id=self.categoria.pk + 999).motivo,
                         "FALLBACK_CATEGORIA_INVALIDA")
        self.categoria.is_active = False
        self.categoria.save()
        self.assertEqual(emparejar_con_fallback(oferta(), categoria_id=self.categoria.pk).motivo,
                         "FALLBACK_CATEGORIA_INVALIDA")

    def test_categoria_o_marca_ausentes_no_consultan_bd(self):
        with self.assertNumQueries(0):
            self.assertEqual(emparejar_con_fallback(oferta(), categoria_id=None).motivo, "FALLBACK_SIN_CATEGORIA")
            self.assertEqual(emparejar_con_fallback(oferta(brand=None), categoria_id=self.categoria.pk).motivo,
                             "FALLBACK_SIN_MARCA")

    def test_snapshot_sinonimos_es_copia_y_no_contamina_cache(self):
        snapshot = obtener_sinonimos()
        snapshot["SW"] = "OTRO"
        self.assertEqual(obtener_sinonimos()["SW"], "SWITCH")

    def test_cambio_de_sinonimos_se_refleja_sin_reescribir_canonico(self):
        SinonimoRed.objects.filter(abreviatura="SW").get().delete()
        before = self.producto.descripcion_norm
        resultado = emparejar_con_fallback(oferta(name="SW 24P POE+"), categoria_id=self.categoria.pk)
        self.assertLess(resultado.score, 100)
        SinonimoRed.objects.create(abreviatura="SW", expansion="SWITCH")
        self.assertEqual(emparejar_con_fallback(oferta(name="SW 24P POE+"), categoria_id=self.categoria.pk).score, 100)
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.descripcion_norm, before)

    def test_auto_match_no_carga_sinonimos(self):
        self.producto.mpn = "MPN-1"
        self.producto.save()
        with patch("apps.catalog.matching.obtener_sinonimos", side_effect=AssertionError("No cargar sinónimos")):
            resultado = emparejar_con_fallback(oferta(), categoria_id=None,
                                             confianza=IdentificadoresConfiables(mpn_confirmado=True))
        self.assertEqual(resultado.estado, EstadoMatch.AUTO_MATCH)

    def test_ambiguedad_m5b_en_otra_categoria_no_consulta_fallback(self):
        self.producto.mpn = "MPN-1"
        self.producto.save()
        otra = Category.objects.create(name="Otra categoría sintética")
        ProductoCanonico.objects.create(marca="Marca X", mpn="MPN-1", descripcion="Otro equipo", categoria=otra)
        with self.assertNumQueries(1), patch(
            "apps.catalog.matching.obtener_sinonimos", side_effect=AssertionError("No cargar sinónimos")
        ):
            resultado = emparejar_con_fallback(
                oferta(), categoria_id=self.categoria.pk,
                confianza=IdentificadoresConfiables(mpn_confirmado=True),
            )
        self.assertEqual(resultado.motivo, "MULTIPLES_CANDIDATOS")

    def test_select_fuzzy_restringido_por_marca_y_categoria(self):
        otra = Category.objects.create(name="Otra categoría sintética")
        ProductoCanonico.objects.create(marca="Marca X", descripcion=self.producto.descripcion, categoria=otra)
        ProductoCanonico.objects.create(marca="Otra marca", descripcion=self.producto.descripcion, categoria=self.categoria)
        with patch("apps.catalog.matching.fuzz.token_set_ratio", wraps=fuzz.token_set_ratio) as puntuar:
            resultado = emparejar_con_fallback(oferta(name="SW 24P POE+"), categoria_id=self.categoria.pk)
        self.assertEqual(puntuar.call_count, 1)
        self.assertEqual(resultado.candidatos, (self.producto.pk,))


class FallbackAsyncTests(TransactionTestCase):
    def setUp(self):
        invalidar_cache()
        self.addCleanup(invalidar_cache)
        self.categoria = Category.objects.create(name="Redes sintéticas")
        ProductoCanonico.objects.create(marca="Marca X", descripcion="SWITCH 24P POE+", categoria=self.categoria)
        SinonimoRed.objects.create(abreviatura="SW", expansion="SWITCH")

    async def test_orm_y_sinonimos_fuera_del_event_loop(self):
        resultado = await aemparejar_con_fallback(oferta(name="SW 24P POE+"), categoria_id=self.categoria.pk)
        self.assertEqual(resultado.score, 100)
        self.assertEqual(resultado.estado, EstadoMatch.REVISION_MANUAL)
