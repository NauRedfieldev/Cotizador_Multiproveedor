"""Fixtures sintéticos: no afirman procedencia de datos reales SYSCOM/Exel."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from apps.catalog.constants import EstadoMatch, MetodoMatch
from apps.catalog.matching import IdentificadoresConfiables, aemparejar, emparejar, resolver_match
from apps.catalog.models import Category, ProductoCanonico
from apps.providers.contracts import ProviderOffer

MPN = IdentificadoresConfiables(mpn_confirmado=True)
MODELO = IdentificadoresConfiables(modelo_confirmado=True)
AMBOS = IdentificadoresConfiables(mpn_confirmado=True, modelo_confirmado=True)


def oferta(**kwargs):
    datos = dict(provider_code="proveedor_sintetico", external_id="123", name="Equipo de prueba",
                 price=Decimal("382.533200"), currency="MXN", brand="Marca X", model="MOD-1", mpn="MPN-1")
    datos.update(kwargs)
    return ProviderOffer(**datos)


def candidato(pk=1, **kwargs):
    datos = dict(id=pk, marca="Marca X", modelo="MOD-1", mpn="MPN-1",
                 descripcion="Equipo sintético", categoria_id=1)
    datos.update(kwargs)
    producto = ProductoCanonico(**datos)
    producto.clean_fields(exclude={"categoria"})
    return producto


class MatchingUnitTests(SimpleTestCase):
    def test_mpn_unico_y_score(self):
        resultado = resolver_match(oferta(), [candidato()], confianza=MPN)
        self.assertEqual(resultado.estado, EstadoMatch.AUTO_MATCH)
        self.assertEqual(resultado.metodo, MetodoMatch.DETERMINISTICO_MPN)
        self.assertEqual(resultado.score, 100)
        self.assertEqual(resultado.canonico.pk, 1)

    def test_sku_confirmado_se_entrega_explicito(self):
        confianza = IdentificadoresConfiables(sku_fabricante_confirmado="FAB-1")
        resultado = resolver_match(oferta(mpn=None), [candidato(sku_fabricante="FAB-1")], confianza=confianza)
        self.assertEqual(resultado.metodo, MetodoMatch.DETERMINISTICO_SKU)
        self.assertEqual(resultado.estado, EstadoMatch.AUTO_MATCH)

    def test_marca_modelo_y_prioridad(self):
        resultado = resolver_match(oferta(), [candidato()], confianza=MODELO)
        self.assertEqual(resultado.metodo, MetodoMatch.DETERMINISTICO_MODELO)
        resultado = resolver_match(oferta(), [candidato()], confianza=AMBOS)
        self.assertEqual(resultado.metodo, MetodoMatch.DETERMINISTICO_MPN)

    def test_prioridad_sku_sobre_modelo(self):
        confianza = IdentificadoresConfiables(modelo_confirmado=True, sku_fabricante_confirmado="FAB-1")
        resultado = resolver_match(oferta(), [candidato(sku_fabricante="FAB-1")], confianza=confianza)
        self.assertEqual(resultado.metodo, MetodoMatch.DETERMINISTICO_SKU)

    def test_sin_confianza_no_se_infiere_por_proveedor(self):
        for proveedor in ["syscom", "exel", "otro"]:
            with self.subTest(proveedor=proveedor):
                resultado = resolver_match(oferta(provider_code=proveedor), [candidato()])
                self.assertEqual(resultado.estado, EstadoMatch.SIN_MATCH)
                self.assertIsNone(resultado.score)

    def test_sin_coincidencias(self):
        resultado = resolver_match(oferta(mpn="OTRO"), [candidato()], confianza=MPN)
        self.assertEqual(resultado.motivo, "SIN_COINCIDENCIA_DETERMINISTICA")

    def test_campos_ausentes_y_no_normalizables(self):
        for valor in [None, "", "   ", "---"]:
            with self.subTest(valor=valor):
                resultado = resolver_match(oferta(mpn=valor), [candidato()], confianza=MPN)
                self.assertEqual(resultado.motivo, "SIN_IDENTIFICADORES_CONFIABLES")

    def test_salta_mpn_ausente(self):
        resultado = resolver_match(oferta(mpn=None), [candidato()], confianza=AMBOS)
        self.assertEqual(resultado.estado, EstadoMatch.AUTO_MATCH)
        self.assertEqual(resultado.metodo, MetodoMatch.DETERMINISTICO_MODELO)

    def test_otra_marca_no_se_asocia(self):
        resultado = resolver_match(oferta(brand="Otra marca"), [candidato()], confianza=MPN)
        self.assertEqual(resultado.estado, EstadoMatch.SIN_MATCH)

    def test_marca_ausente_impide_automatico(self):
        resultado = resolver_match(oferta(brand=None), [candidato()], confianza=MPN)
        self.assertEqual(resultado.motivo, "MARCA_AUSENTE")
        resultado = resolver_match(oferta(brand=None), [candidato()], confianza=MODELO)
        self.assertEqual(resultado.estado, EstadoMatch.SIN_MATCH)

    def test_duplicados_no_se_resuelven_por_orden(self):
        for candidatos in [[candidato(1), candidato(2)], [candidato(2), candidato(1)]]:
            resultado = resolver_match(oferta(), candidatos, confianza=AMBOS)
            self.assertEqual(resultado.estado, EstadoMatch.REVISION_MANUAL)
            self.assertEqual(resultado.candidatos, (1, 2))
            self.assertIsNone(resultado.canonico)
            self.assertIsNone(resultado.score)

    def test_misma_instancia_repetida_no_es_ambiguo(self):
        producto = candidato()
        self.assertEqual(resolver_match(oferta(), [producto, producto], confianza=MPN).estado, EstadoMatch.AUTO_MATCH)

    def test_colision_por_parentesis(self):
        resultado = resolver_match(oferta(mpn="ABC-PC"), [candidato(mpn="ABC-P(C)")], confianza=MPN)
        self.assertEqual(resultado.motivo, "COLISION_DE_NORMALIZACION")

    def test_separadores_no_aprobados_requieren_revision(self):
        for mpn in ["MPN 1", "MPN/1", "MPN1"]:
            with self.subTest(mpn=mpn):
                self.assertEqual(resolver_match(oferta(mpn=mpn), [candidato()], confianza=MPN).estado,
                                 EstadoMatch.REVISION_MANUAL)

    def test_mayusculas_y_espacios_exteriores_se_aceptan(self):
        resultado = resolver_match(oferta(mpn=" mpn-1 ", brand=" marca x "), [candidato()], confianza=MPN)
        self.assertEqual(resultado.estado, EstadoMatch.AUTO_MATCH)

    def test_colision_de_marca(self):
        resultado = resolver_match(oferta(brand="Marca@X"), [candidato()], confianza=MPN)
        self.assertEqual(resultado.motivo, "COLISION_DE_MARCA")

    def test_identificadores_apuntan_a_distintos_candidatos(self):
        resultado = resolver_match(oferta(), [candidato(1, modelo="OTRO"), candidato(2, mpn="OTRO")], confianza=AMBOS)
        self.assertEqual(resultado.motivo, "IDENTIFICADORES_CONTRADICTORIOS")
        self.assertEqual(resultado.candidatos, (1, 2))

    def test_clave_sin_acierto_contradice_al_elegido(self):
        resultado = resolver_match(oferta(mpn="OTRO"), [candidato()], confianza=AMBOS)
        self.assertEqual(resultado.motivo, "IDENTIFICADORES_CONTRADICTORIOS")

    def test_identificador_ausente_en_canonico_no_es_conflicto(self):
        resultado = resolver_match(oferta(), [candidato(mpn="")], confianza=AMBOS)
        self.assertEqual(resultado.estado, EstadoMatch.AUTO_MATCH)

    def test_modelo_no_confiable_no_bloquea_mpn(self):
        resultado = resolver_match(oferta(model="NO CONFIRMADO"), [candidato()], confianza=MPN)
        self.assertEqual(resultado.estado, EstadoMatch.AUTO_MATCH)

    def test_no_lee_raw_precio_stock_ni_external_id(self):
        class PayloadProhibido(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("No leer JSON crudo")

            def __getitem__(self, key):
                raise AssertionError("No leer JSON crudo")

        a = oferta(raw=PayloadProhibido())
        b = replace(a, external_id="otro", price=Decimal("1"), currency="USD", stock=0)
        self.assertEqual(resolver_match(a, [candidato()], confianza=MPN),
                         resolver_match(b, [candidato()], confianza=MPN))

    def test_rechaza_confianza_o_identificadores_de_tipo_invalido(self):
        with self.assertRaises(TypeError):
            IdentificadoresConfiables(mpn_confirmado="si")
        with self.assertRaises(TypeError):
            IdentificadoresConfiables(sku_fabricante_confirmado=123)
        with self.assertRaises(TypeError):
            resolver_match(oferta(mpn=123), [candidato()], confianza=MPN)

    def test_candidato_sin_id_rechazado(self):
        producto = candidato()
        producto.pk = None
        with self.assertRaises(ValueError):
            resolver_match(oferta(), [producto], confianza=MPN)


class MatchingDatabaseTests(TestCase):
    def setUp(self):
        self.producto = ProductoCanonico.objects.create(
            marca="Marca X", mpn="MPN-1", modelo="MOD-1", descripcion="Equipo sintético",
            categoria=Category.objects.create(name="Redes sintéticas"),
        )

    def test_consulta_unica_y_sin_escrituras_o_red(self):
        with patch("socket.socket", side_effect=AssertionError("No consultar red")), CaptureQueriesContext(connection) as consultas:
            resultado = emparejar(oferta(), confianza=AMBOS)
        self.assertEqual(resultado.canonico.pk, self.producto.pk)
        self.assertEqual(len(consultas), 1)
        self.assertTrue(consultas[0]["sql"].lstrip().startswith("SELECT"))
        self.assertEqual(ProductoCanonico.objects.count(), 1)

    def test_no_consulta_bd_sin_identificadores(self):
        with self.assertNumQueries(0):
            self.assertEqual(emparejar(oferta()).estado, EstadoMatch.SIN_MATCH)

    def test_colisiones_en_bd_se_envian_a_revision(self):
        ProductoCanonico.objects.create(
            marca="Marca X", mpn="MPN1", descripcion="Variante sintética",
            categoria=self.producto.categoria,
        )
        resultado = emparejar(oferta(), confianza=MPN)
        self.assertEqual(resultado.motivo, "MULTIPLES_CANDIDATOS")
        self.assertEqual(len(resultado.candidatos), 2)


class MatchingAsyncTests(TransactionTestCase):
    def setUp(self):
        self.producto = ProductoCanonico.objects.create(
            marca="Marca X", mpn="MPN-1", descripcion="Equipo sintético",
            categoria=Category.objects.create(name="Redes sintéticas"),
        )

    async def test_consulta_orm_fuera_del_event_loop(self):
        resultado = await aemparejar(oferta(), confianza=MPN)
        self.assertEqual(resultado.estado, EstadoMatch.AUTO_MATCH)
        self.assertEqual(resultado.canonico.pk, self.producto.pk)
