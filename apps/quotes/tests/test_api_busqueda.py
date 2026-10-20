"""Pruebas A1–A9 de la búsqueda JSON de M7a (docs/28), sin red ni credenciales reales.

Usa el mismo flujo que el comparador; aquí se prueba el contrato JSON de docs/27 §B2 y las
ofertas guardadas en sesión. El modo degradado en sí ya lo cubren V1–V16.
"""
import json
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings, tag
from django.urls import reverse

from apps.catalog.models import Category, ProductoCanonico
from apps.providers.models import Provider
from apps.providers.tests.soporte import adaptadores_registrados, crear_proveedor, sin_red
from apps.quotes import api
from apps.quotes.tests.test_vistas_cotizacion import adaptador, crear_asesor, mxn, usd
from apps.quotes.views import LIMITE_COTIZACIONES
from apps.quotes.tests.test_commercial_persistence import CommercialFixtures
from apps.quotes.tests.test_api_cotizaciones import alta_comercial
from apps.quotes.models import QuoteSelectionContext
from uuid import uuid4


@tag("django_db")
@override_settings(OFERTAS_POR_PROVEEDOR=3)
class ApiBusquedaTests(TestCase):
    def setUp(self):
        cache.clear()
        crear_proveedor("alfa")
        crear_proveedor("beta")
        self.url = reverse("api_buscar_productos")
        actor = crear_asesor()
        actor.user_permissions.add(Permission.objects.get(content_type__app_label="quotes", codename="view_commercial_costs"))
        self.async_client.force_login(actor)

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
            "product_url": None,
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

    async def test_a1b_el_cotizador_recibe_primero_lo_que_el_titulo_nombra(self):
        _, cuerpo = await self.buscar([
            adaptador("alfa",
                      mxn("sim", "50.00", nombre="Reactivación de Conectividad / SIM"),
                      mxn("monitor", "900.00", nombre='Monitor LED 24" / HDMI'),
                      mxn("soporte", "100.00", nombre="Soporte de Pared para Monitor / VESA")),
            adaptador("beta"),
        ], q="monitor")
        self.assertEqual([d["external_id"] for d in cuerpo["data"]], ["monitor", "soporte", "sim"])
        self.assertEqual([d["es_mejor_precio"] for d in cuerpo["data"]], [True, False, False])

    async def test_a1c_el_cotizador_recibe_la_ficha_pero_no_la_foto(self):
        _, cuerpo = await self.buscar([
            adaptador("alfa", mxn("1", "100.00", foto="https://img.ejemplo.invalid/1.png",
                                  ficha="https://www.ejemplo.invalid/producto/1.html")),
            adaptador("beta"),
        ])
        self.assertEqual(cuerpo["data"][0]["product_url"], "https://www.ejemplo.invalid/producto/1.html")
        self.assertNotIn("image_url", cuerpo["data"][0])

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

    async def test_a11_sin_sesion_da_401_json_sin_consultar_ni_contar(self):
        await self.async_client.alogout()
        llamadas = []
        respuesta, cuerpo = await self.buscar([adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)])
        self.assertEqual(respuesta.status_code, 401)
        self.assertEqual(cuerpo, {"success": False, "error": "Inicia sesión para continuar.",
                                "code": "UNAUTHENTICATED", "details": {}})
        self.assertEqual(llamadas, [])
        self.assertIsNone(await cache.aget("cotizar:127.0.0.1"))


@override_settings(CCONOR_EMPRESA={"name": "QA sintética", "rfc": "", "tagline": "", "phone": "", "email": ""},
                   CCONOR_PROVIDER_PRICE_BASES={"alfa": {"vat_basis": "NET"}})
class CommercialSearchTests(CommercialFixtures, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        crear_proveedor("alfa")
        self.client.force_login(self.actor)

    def search(self, price="100", *, currency="MXN", **params):
        offer = usd("1", price) if currency == "USD" else mxn("1", price)
        with adaptadores_registrados(adaptador("alfa", offer)), sin_red():
            response = self.client.get(reverse("api_buscar_productos"), {"q": "equipo", "schema_version": "2", **params})
        self.assertEqual(response.status_code, 200, response.content.decode())
        return response.json()

    def body(self, result):
        return alta_comercial(items=[{"line_key": str(uuid4()), "source": {"type": "API",
            "context_ref": result["context_ref"], "offer_ref": result["data"][0]["offer_ref"]}, "quantity": 1,
            "units": {"source_label": "Pieza", "sale_label": "Pieza", "conversion_factor": "1", "confirmed": True},
            "pricing": {"mode": "MARGIN", "margin_percent": "35"}}])

    def post(self, data, status=201):
        response = self.client.post(reverse("api_crear_cotizacion"), json.dumps(data), content_type="application/json")
        self.assertEqual(response.status_code, status, response.content.decode())
        return response.json().get("data", response.json())

    def test_busqueda_nueva_no_sustituye_captura_anterior(self):
        first = self.search()
        second = self.search("110")
        self.assertNotEqual(first["context_ref"], second["context_ref"])
        old = QuoteSelectionContext.objects.get(pk=first["context_ref"])
        self.assertEqual(list(old.payload["offers"].values())[0]["price"], "100.000000")
        saved = self.post(self.body(first))
        from apps.quotes.models import QuoteRevision
        self.assertEqual(QuoteRevision.objects.get(quote__folio=saved["folio"]).snapshots.get().source_unit_price, Decimal("100"))

    def test_originales_api_falsificados_no_se_aceptan(self):
        result = self.search()
        for field in ("price", "currency", "provider", "raw"):
            body = self.body(result)
            body["items"][0][field] = "falso"
            self.post(body, 400)
        body = self.body(result)
        body["items"][0]["cost"] = {"amount": "1", "currency": "MXN", "vat_basis": "NET"}
        self.post(body, 400)

    def test_captura_ajena_y_referencia_inventada_no_crean_quote(self):
        result = self.search()
        body = self.body(result)
        self.client.force_login(self.other)
        self.post(body, 409)
        self.client.force_login(self.actor)
        body["items"][0]["source"]["offer_ref"] = str(uuid4())
        self.post(body, 409)

    def test_base_iva_desconocida_no_se_asume_neta(self):
        with override_settings(CCONOR_PROVIDER_PRICE_BASES={}):
            result = self.search()
        self.assertEqual(result["data"][0]["vat"], {"vat_basis": "UNKNOWN"})
        self.post(self.body(result), 409)

    def test_seleccion_guardada_se_edita_sin_busqueda_ni_sesion(self):
        body = self.body(self.search())
        created = self.post(body)
        edit = {k: v for k, v in body.items() if k not in ("request_id", "schema_version", "notes")}
        edit["items"][0]["source"] = {"type": "EXISTING"}
        edit["items"][0]["quantity"] = 2
        edit["edit_version"] = 1
        with mock.patch.object(api, "_owned_context", side_effect=AssertionError("No recapturar")):
            response = self.client.put(reverse("api_guardar_revision", args=[created["folio"], 1]),
                                       json.dumps(edit), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content.decode())

    def test_reintentar_alta_no_necesita_captura_original(self):
        body = self.body(self.search())
        first = self.post(body)
        with mock.patch.object(api, "_owned_context", side_effect=AssertionError("No recapturar")):
            again = self.post(body)
        self.assertEqual(again["folio"], first["folio"])

    def test_captura_excluye_raw_y_contextos_son_por_usuario(self):
        result = self.search()
        context = QuoteSelectionContext.objects.get(pk=result["context_ref"])
        self.assertEqual(context.actor_id, self.actor.pk)
        self.assertNotIn("raw", json.dumps(context.payload))
        from django.core.exceptions import ValidationError
        offer = next(iter(context.payload["offers"].values()))
        offer["vat"]["raw"] = {"dato": "no guardar"}
        with self.assertRaises(ValidationError):
            QuoteSelectionContext.objects.create(actor=self.actor, kind="SEARCH", payload=context.payload)

    def test_fx_de_cabecera_no_se_mezcla_por_fechas_o_busquedas_distintas(self):
        first = self.search(currency="USD", tipo_cambio="17.50")
        second = self.search(currency="USD", tipo_cambio="18.00")
        body = self.body(first)
        body["fx_context_id"] = first["fx_context_id"]
        body["items"].extend(self.body(second)["items"])
        saved = self.post(body)
        from apps.quotes.models import QuoteRevision
        revision = QuoteRevision.objects.get(quote__folio=saved["folio"])
        self.assertEqual(revision.exchange_rate, Decimal("17.50"))
        response = self.client.get(reverse("api_guardar_revision", args=[saved["folio"], 1]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["preview"]["resultado"]["total"], "6246.16")

    def test_fx_anterior_se_conserva_al_editar_sin_permisos_para_cambiarlo(self):
        result = self.search(currency="USD", tipo_cambio="17.50")
        body = self.body(result)
        body["fx_context_id"] = result["fx_context_id"]
        created = self.post(body)
        self.actor.groups.remove(self.commercial_admin)
        edit = {k: v for k, v in body.items() if k not in ("request_id", "schema_version", "notes")}
        edit["items"][0]["source"] = {"type": "EXISTING"}
        edit["items"][0]["quantity"] = 2
        edit["edit_version"] = 1
        response = self.client.put(reverse("api_guardar_revision", args=[created["folio"], 1]),
                                   json.dumps(edit), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content.decode())
