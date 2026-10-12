"""Pruebas V1–V12 de la vista async de cotización en modo degradado (M7, docs/24).

Los proveedores son adaptadores de prueba cuyo fetch no hace HTTP; sin_red() corta cualquier
intento de salir a la red. No se usa ninguna credencial real.
"""
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings, tag
from django.urls import reverse

from apps.catalog.constants import EstadoMatch, MetodoMatch
from apps.catalog.models import Category, ProductoCanonico
from apps.providers.models import Provider
from apps.providers.tests.soporte import (
    adaptadores_registrados,
    crear_adaptador,
    crear_proveedor,
    producto,
    sin_red,
)
from apps.quotes.views import LIMITE_COTIZACIONES

PLANTILLA = "comparador.html"


def mxn(id_, precio, **campos):
    return producto(id_, precio=precio, moneda="MXN", **campos)


def usd(id_, precio, **campos):
    return producto(id_, precio=precio, moneda="USD", **campos)


def adaptador(code, *items, error=None, llamadas=None):
    """Adaptador sin HTTP: devuelve items o lanza error. Anota cada consulta en llamadas."""

    async def fetch(self, query, client):
        if llamadas is not None:
            llamadas.append(code)
        if error is not None:
            raise error
        return list(items)

    return crear_adaptador(code, fetch=fetch)


@tag("django_db")
@override_settings(OFERTAS_POR_PROVEEDOR=3)
class VistaCotizacionTests(TestCase):
    def setUp(self):
        cache.clear()
        crear_proveedor("alfa")
        crear_proveedor("beta")

    async def buscar(self, adaptadores, **parametros):
        parametros.setdefault("q", "camara ip")
        with adaptadores_registrados(*adaptadores), sin_red():
            return await self.async_client.get(reverse("cotizar_productos"), parametros)

    def assertSinTraza(self, respuesta):
        self.assertTemplateUsed(respuesta, PLANTILLA)
        self.assertNotIn(b"Traceback", respuesta.content)

    async def test_v1_un_proveedor_caido_da_200_con_aviso(self):
        respuesta = await self.buscar([
            adaptador("alfa", mxn("1", "100.00")),
            adaptador("beta", error=TimeoutError()),
        ])
        self.assertEqual(respuesta.status_code, 200)
        self.assertSinTraza(respuesta)
        self.assertEqual(respuesta.context["proveedores_caidos"], ["beta"])
        self.assertEqual([o.oferta.provider_code for o in respuesta.context["ofertas"]], ["alfa"])
        self.assertNotIn("error", respuesta.context)

    async def test_v2_ambos_caidos_da_503_sin_traza(self):
        respuesta = await self.buscar([
            adaptador("alfa", error=TimeoutError()),
            adaptador("beta", error=TimeoutError()),
        ])
        self.assertEqual(respuesta.status_code, 503)
        self.assertSinTraza(respuesta)
        self.assertEqual(respuesta.context["proveedores_caidos"], ["alfa", "beta"])
        self.assertEqual(respuesta.context["ofertas"], [])
        self.assertIn("error", respuesta.context)

    async def test_v3_ambos_vivos_ordenados_por_precio_comparable(self):
        respuesta = await self.buscar([
            adaptador("alfa", mxn("1", "300.00"), mxn("2", "100.00")),
            adaptador("beta", mxn("1", "200.00")),
        ])
        self.assertEqual(respuesta.status_code, 200)
        ofertas = respuesta.context["ofertas"]
        self.assertEqual(
            [(o.oferta.provider_code, o.precio_comparable) for o in ofertas],
            [("alfa", Decimal("100.00")), ("beta", Decimal("200.00")), ("alfa", Decimal("300.00"))],
        )
        self.assertEqual([o.es_mejor_precio for o in ofertas], [True, True, False])
        self.assertEqual(respuesta.context["proveedores_caidos"], [])

    async def test_v4_usd_exige_tipo_de_cambio_y_convierte_exacto(self):
        adaptadores = [adaptador("alfa", usd("1", "10.25")), adaptador("beta", mxn("1", "200.00"))]
        sin_tipo = await self.buscar(adaptadores)
        self.assertEqual(sin_tipo.status_code, 400)
        self.assertSinTraza(sin_tipo)
        self.assertIn("tipo de cambio", sin_tipo.context["error"])

        con_tipo = await self.buscar(adaptadores, tipo_cambio=" 17.50 ")
        self.assertEqual(con_tipo.status_code, 200)
        tipo = con_tipo.context["tipo_cambio"]
        self.assertEqual((tipo.valor, tipo.origen), (Decimal("17.50"), "manual"))
        self.assertEqual(
            [(o.oferta.provider_code, o.precio_comparable) for o in con_tipo.context["ofertas"]],
            [("alfa", Decimal("179.3750")), ("beta", Decimal("200.00"))],
        )

    async def test_v5_entrada_invalida_da_400_sin_consultar_proveedores(self):
        invalidas = [
            {"q": ""},
            {"q": "   ", "sku": "  "},
            {"tipo_cambio": "17,50"},
            {"tipo_cambio": "0"},
            {"categoria": "abc"},
            {"categoria": "0"},
            {"categoria": "-3"},
        ]
        for parametros in invalidas:
            with self.subTest(parametros=parametros):
                cache.clear()
                llamadas = []
                respuesta = await self.buscar(
                    [adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)], **parametros
                )
                self.assertEqual(respuesta.status_code, 400)
                self.assertSinTraza(respuesta)
                self.assertIn("error", respuesta.context)
                self.assertEqual(llamadas, [])

    async def test_v6_ningun_proveedor_activo_da_503(self):
        await Provider.objects.aupdate(active=False)
        respuesta = await self.buscar([adaptador("alfa", mxn("1", "1.00"))])
        self.assertEqual(respuesta.status_code, 503)
        self.assertSinTraza(respuesta)
        self.assertEqual(respuesta.context["proveedores_caidos"], [])

    async def test_v7_rate_limit_da_429_sin_consultar_proveedores(self):
        llamadas = []
        adaptadores = [adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)]
        for _ in range(LIMITE_COTIZACIONES):
            self.assertEqual((await self.buscar(adaptadores)).status_code, 200)
        respuesta = await self.buscar(adaptadores)
        self.assertEqual(respuesta.status_code, 429)
        self.assertSinTraza(respuesta)
        self.assertEqual(len(llamadas), LIMITE_COTIZACIONES)

    async def test_v7b_clave_caducada_entre_aadd_y_aincr_cuenta_como_primera(self):
        with mock.patch.object(cache, "aincr", side_effect=ValueError("Key not found")):
            respuesta = await self.buscar([adaptador("alfa", mxn("1", "1.00"))])
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(await cache.aget("cotizar:127.0.0.1"), 1)

    async def test_v8_excepcion_inesperada_de_un_adaptador_queda_degradada(self):
        with self.assertLogs("apps.providers", "ERROR"):
            respuesta = await self.buscar([
                adaptador("alfa", mxn("1", "1.00")),
                adaptador("beta", error=RuntimeError("fallo interno")),
            ])
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["proveedores_caidos"], ["beta"])
        self.assertNotIn(b"fallo interno", respuesta.content)

    async def test_v9_datos_que_m6_rechaza_dejan_fuera_solo_a_ese_proveedor(self):
        duplicados = adaptador("alfa", mxn("1", "100.00"), mxn("1", "90.00"))
        with self.assertLogs("apps.quotes", "WARNING"):
            respuesta = await self.buscar([duplicados, adaptador("beta", mxn("7", "50.00"))])
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["proveedores_caidos"], ["alfa"])
        self.assertEqual([o.oferta.provider_code for o in respuesta.context["ofertas"]], ["beta"])

        cache.clear()
        with self.assertLogs("apps.quotes", "WARNING"):
            todos = await self.buscar([duplicados, adaptador("beta", error=TimeoutError())])
        self.assertEqual(todos.status_code, 503)
        self.assertEqual(todos.context["proveedores_caidos"], ["alfa", "beta"])

    async def test_v10_confianza_por_proveedor_en_el_emparejamiento(self):
        categoria = await Category.objects.acreate(name="Redes sintéticas")
        canonico = await ProductoCanonico.objects.acreate(
            marca="Marca X", modelo="MOD-1", descripcion="Equipo sintético", categoria=categoria
        )
        await Provider.objects.acreate(code="syscom", name="Syscom", base_url="https://syscom.example.com")
        item = mxn("1", "100.00", marca="Marca X", modelo="MOD-1")
        respuesta = await self.buscar([
            adaptador("syscom", item), adaptador("alfa", item), adaptador("beta"),
        ])
        self.assertEqual(respuesta.status_code, 200)
        matches = {o.oferta.provider_code: o.match for o in respuesta.context["ofertas"]}
        self.assertEqual(matches["syscom"].estado, EstadoMatch.AUTO_MATCH)
        self.assertEqual(matches["syscom"].metodo, MetodoMatch.DETERMINISTICO_MODELO)
        self.assertEqual(matches["syscom"].canonico.pk, canonico.pk)
        # Un proveedor sin declaración de confianza no autoriza ninguna clave; sin categoría
        # tampoco hay fallback M5c.
        self.assertEqual(matches["alfa"].estado, EstadoMatch.SIN_MATCH)
        self.assertIsNone(matches["alfa"].canonico)
        self.assertEqual(matches["alfa"].motivo, "FALLBACK_SIN_CATEGORIA")

    async def test_v10b_categoria_llega_al_fallback_de_m5(self):
        item = mxn("1", "100.00", marca="Marca X", nombre="Switch 24 puertos")
        respuesta = await self.buscar([adaptador("alfa", item), adaptador("beta")], categoria="999")
        self.assertEqual(respuesta.status_code, 200)
        # M5c valida la categoría en BD: la 999 no existe.
        self.assertEqual(respuesta.context["ofertas"][0].match.motivo, "FALLBACK_CATEGORIA_INVALIDA")

    async def test_v11_solo_get(self):
        llamadas = []
        with adaptadores_registrados(adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)), sin_red():
            respuesta = await self.async_client.post(reverse("cotizar_productos"), {"q": "camara"})
        self.assertEqual(respuesta.status_code, 405)
        self.assertEqual(llamadas, [])

    @override_settings(OFERTAS_POR_PROVEEDOR=1)
    async def test_v12_solo_se_muestran_las_mejores_n_por_proveedor(self):
        respuesta = await self.buscar([
            adaptador("alfa", mxn("1", "300.00"), mxn("2", "100.00")),
            adaptador("beta", mxn("1", "200.00"), mxn("2", "250.00")),
        ])
        self.assertEqual(
            [(o.oferta.provider_code, o.oferta.external_id) for o in respuesta.context["ofertas"]],
            [("alfa", "2"), ("beta", "1")],
        )
