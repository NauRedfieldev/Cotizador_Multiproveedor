"""Pruebas A1–A9 de la búsqueda JSON de M7a (docs/28), sin red ni credenciales reales.

Usa el mismo flujo que el comparador; aquí se prueba el contrato JSON de docs/27 §B2 y las
ofertas guardadas en sesión. El modo degradado en sí ya lo cubren V1–V16.
"""
import json
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings, tag
from django.urls import reverse

from apps.catalog.models import Category, ProductoCanonico
from apps.providers.models import Provider
from apps.providers.tests.soporte import adaptadores_registrados, crear_proveedor, sin_red
from apps.quotes import api
from apps.quotes.tests.test_vistas_cotizacion import adaptador, mxn, usd
from apps.quotes.views import LIMITE_COTIZACIONES


@tag("django_db")
@override_settings(OFERTAS_POR_PROVEEDOR=3)
class ApiBusquedaTests(TestCase):
    def setUp(self):
        cache.clear()
        crear_proveedor("alfa")
        crear_proveedor("beta")
        self.url = reverse("api_buscar_productos")

    async def buscar(self, adaptadores, **parametros):
        parametros.setdefault("q", "camara ip")
        with adaptadores_registrados(*adaptadores), sin_red():
            respuesta = await self.async_client.get(self.url, parametros)
        self.assertEqual(respuesta["Content-Type"], "application/json")
        return respuesta, json.loads(respuesta.content, parse_float=Decimal)

    async def ofertas_en_sesion(self):
        sesion = await self.async_client.asession()
        return await sesion.aget(api.CLAVE_OFERTAS, {})

    async def test_a1_respuesta_200_con_el_contrato_de_b2(self):
        respuesta, cuerpo = await self.buscar([
            adaptador("alfa", usd("7", "10.25", marca="Marca", modelo="M-1", existencia=0)),
            adaptador("beta", mxn("9", "200.00")),
        ], tipo_cambio="17.50")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(
            {k: cuerpo[k] for k in ("success", "error", "count", "proveedores_caidos", "proveedores_caidos_nombres")},
            {"success": True, "error": None, "count": 2, "proveedores_caidos": [], "proveedores_caidos_nombres": []},
        )
        self.assertEqual(
            {k: cuerpo["tipo_cambio"][k] for k in ("valor", "origen")}, {"valor": "17.50", "origen": "manual"}
        )
        primera = cuerpo["data"][0]
        self.assertEqual(primera, {
            "provider": "alfa",
            "provider_label": "Alfa",
            "external_id": "7",
            "name": primera["name"],
            "brand": "Marca",
            "model": "M-1",
            "mpn": primera["mpn"],
            "price_original": "10.25",
            "currency": "USD",
            "precio_mxn": "179.3750",
            "es_mejor_precio": True,
            "stock": 0,
            "match": {
                "estado": "SIN_MATCH",
                "estado_label": "Sin coincidencia",
                "canonico": None,
                "score": None,
                "motivo": "FALLBACK_SIN_CATEGORIA",
            },
        })
        # Ya viene ordenada por precio en MXN; el dinero siempre es texto.
        self.assertEqual([d["precio_mxn"] for d in cuerpo["data"]], ["179.3750", "200.00"])
        for d in cuerpo["data"]:
            self.assertIsInstance(d["price_original"], str)
            self.assertIsInstance(d["precio_mxn"], str)

    async def test_a2_auto_match_incluye_el_canonico(self):
        categoria = await Category.objects.acreate(name="Redes sintéticas")
        canonico = await ProductoCanonico.objects.acreate(
            marca="Marca X", modelo="MOD-1", descripcion="Equipo sintético", categoria=categoria
        )
        await Provider.objects.acreate(code="syscom", name="Syscom", base_url="https://syscom.example.com")
        _, cuerpo = await self.buscar([
            adaptador("syscom", mxn("1", "100.00", marca="Marca X", modelo="MOD-1")),
            adaptador("alfa"), adaptador("beta"),
        ])
        match = cuerpo["data"][0]["match"]
        self.assertEqual((match["estado"], match["estado_label"]), ("AUTO_MATCH", "Coincidencia automática"))
        self.assertEqual(match["canonico"], {
            "id": canonico.pk, "descripcion": "Equipo sintético", "marca": "Marca X", "modelo": "MOD-1",
        })

    async def test_a3_errores_de_entrada_dan_400_con_el_mismo_cuerpo_sin_consultar(self):
        for parametros in ({"q": " ", "sku": ""}, {"tipo_cambio": "17,50"}, {"categoria": "abc"}):
            with self.subTest(parametros=parametros):
                llamadas = []
                respuesta, cuerpo = await self.buscar(
                    [adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)], **parametros
                )
                self.assertEqual(respuesta.status_code, 400)
                self.assertFalse(cuerpo["success"])
                self.assertTrue(cuerpo["error"])
                self.assertEqual((cuerpo["data"], cuerpo["count"]), ([], 0))
                self.assertEqual(llamadas, [])

    async def test_a4_usd_sin_tipo_de_cambio_da_400(self):
        respuesta, cuerpo = await self.buscar([adaptador("alfa", usd("1", "10.25")), adaptador("beta")])
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("tipo de cambio", cuerpo["error"])
        self.assertIsNone(cuerpo["tipo_cambio"])

    async def test_a5_degradado_200_y_todos_caidos_503(self):
        respuesta, cuerpo = await self.buscar([
            adaptador("alfa", mxn("1", "1.00")), adaptador("beta", error=TimeoutError()),
        ])
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(cuerpo["success"])
        self.assertEqual((cuerpo["proveedores_caidos"], cuerpo["proveedores_caidos_nombres"]), (["beta"], ["Beta"]))

        respuesta, cuerpo = await self.buscar([
            adaptador("alfa", error=TimeoutError()), adaptador("beta", error=TimeoutError()),
        ])
        self.assertEqual(respuesta.status_code, 503)
        self.assertFalse(cuerpo["success"])
        self.assertEqual(cuerpo["proveedores_caidos"], ["alfa", "beta"])

    async def test_a6_el_limite_se_comparte_con_el_comparador(self):
        adaptadores = [adaptador("alfa", mxn("1", "1.00"))]
        with adaptadores_registrados(*adaptadores), sin_red():
            for _ in range(LIMITE_COTIZACIONES):
                html = await self.async_client.get(reverse("cotizar_productos"), {"q": "camara"})
                self.assertEqual(html.status_code, 200)
        respuesta, cuerpo = await self.buscar(adaptadores)
        self.assertEqual(respuesta.status_code, 429)
        self.assertFalse(cuerpo["success"])

    async def test_a7_las_ofertas_devueltas_quedan_en_la_sesion(self):
        await self.buscar([adaptador("alfa", usd("7", "10.25", modelo="M-1")), adaptador("beta")],
                          tipo_cambio="17.50")
        guardada = (await self.ofertas_en_sesion())["alfa:7"]
        self.assertEqual(
            {k: guardada[k] for k in ("provider_code", "provider_name", "external_id", "model", "price",
                                       "currency", "es_mejor_precio")},
            {"provider_code": "alfa", "provider_name": "Alfa", "external_id": "7", "model": "M-1",
             "price": "10.25", "currency": "USD", "es_mejor_precio": True},
        )
        self.assertEqual(
            {k: guardada["tipo_cambio"][k] for k in ("valor", "origen")}, {"valor": "17.50", "origen": "manual"}
        )
        self.assertTrue(guardada["captured_at"])

        # Una búsqueda nueva pisa la entrada con el precio y el tipo de cambio nuevos.
        await self.buscar([adaptador("alfa", usd("7", "11.00")), adaptador("beta")], tipo_cambio="18.00")
        guardada = (await self.ofertas_en_sesion())["alfa:7"]
        self.assertEqual((guardada["price"], guardada["tipo_cambio"]["valor"]), ("11.00", "18.00"))

    async def test_a8_un_error_no_guarda_ni_borra_ofertas(self):
        await self.buscar([adaptador("alfa", mxn("1", "1.00")), adaptador("beta")])
        await self.buscar([adaptador("alfa", error=TimeoutError()), adaptador("beta", error=TimeoutError())])
        self.assertEqual(list(await self.ofertas_en_sesion()), ["alfa:1"])

    async def test_a9_la_sesion_conserva_solo_las_mas_recientes(self):
        with mock.patch.object(api, "MAX_OFERTAS_EN_SESION", 2):
            await self.buscar([adaptador("alfa", mxn("1", "1.00"), mxn("2", "2.00")), adaptador("beta")])
            await self.buscar([adaptador("alfa", mxn("1", "1.00"), mxn("3", "3.00")), adaptador("beta")])
        self.assertEqual(list(await self.ofertas_en_sesion()), ["alfa:1", "alfa:3"])

    async def test_a10_solo_get(self):
        respuesta = await self.async_client.post(self.url, {"q": "camara"})
        self.assertEqual(respuesta.status_code, 405)
