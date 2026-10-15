"""Pruebas C1–C16 de la API de cotizaciones de M7a sobre M8 (docs/28), sin red.

Datos sintéticos y PDF en un directorio temporal, como test_snapshots. Las ofertas se ponen
directamente en la sesión, salvo en C1, que recorre búsqueda → alta con el flujo real.
"""
import json
import tempfile
import threading
from decimal import Decimal
from pathlib import Path
from unittest import mock
from uuid import uuid4

from django.core.cache import cache
from asgiref.sync import sync_to_async
from django.test import AsyncClient, TestCase, TransactionTestCase, override_settings, tag
from django.urls import reverse

from apps.providers.tests.soporte import adaptadores_registrados, crear_proveedor, ejecutar_en_hilos, sin_red
from apps.quotes import api
from apps.quotes.models import Quote, QuoteRevision
from apps.quotes.snapshots import ArchivedPDFError
from apps.quotes.tests.test_vistas_cotizacion import adaptador, crear_asesor, usd

TIPO = {"valor": "17.50", "origen": "manual", "fecha": "2026-10-15T18:00:00+00:00"}


def oferta_guardada(provider, external_id, precio, moneda="USD", tipo=TIPO):
    return {
        "provider_code": provider, "provider_name": provider.title(), "external_id": external_id,
        "name": f"Equipo sintético {external_id}", "brand": "Marca", "model": f"M-{external_id}", "mpn": "",
        "price": precio, "currency": moneda, "stock": None, "es_mejor_precio": True,
        "tipo_cambio": tipo, "captured_at": "2026-10-15T18:00:00+00:00",
    }


def cuerpo_alta(**cambios):
    datos = {
        "request_id": str(uuid4()),
        "client_name": "Cliente sintético S.A. de C.V.",
        "client_rfc": "CSI980415AA2",
        "client_email": "compras@cliente.example.com",
        "valid_days": 15,
        "notes": "Entrega en sitio.\n\nPago contra entrega.",
        "items": [{"provider": "alfa", "external_id": "7", "quantity": "2"}],
    }
    datos.update(cambios)
    return datos


@tag("django_db")
class ApiCotizacionesTests(TestCase):
    def setUp(self):
        cache.clear()
        tmp = tempfile.TemporaryDirectory(prefix="m7a-")
        self.addCleanup(tmp.cleanup)
        self.pdfs = Path(tmp.name)
        self.enterContext(override_settings(QUOTE_PDF_ROOT=tmp.name))
        self.asesor = crear_asesor()
        self.otro = crear_asesor("otro")
        self.staff = crear_asesor("jefa", is_staff=True)
        crear_proveedor("alfa")
        crear_proveedor("beta")
        self.async_client.force_login(self.asesor)
        sesion = self.async_client.session
        sesion[api.CLAVE_OFERTAS] = {
            "alfa:7": oferta_guardada("alfa", "7", "10.25"),
            "beta:9": oferta_guardada("beta", "9", "200.00", moneda="MXN", tipo=None),
            "beta:10": oferta_guardada("beta", "10", "5.00", tipo={**TIPO, "valor": "18.00"}),
        }
        sesion.save()

    async def enviar(self, metodo, url, datos, cliente=None):
        cliente = cliente or self.async_client
        respuesta = await getattr(cliente, metodo)(url, data=json.dumps(datos), content_type="application/json")
        return respuesta, json.loads(respuesta.content)

    async def alta(self, **cambios):
        return await self.enviar("post", reverse("api_crear_cotizacion"), cuerpo_alta(**cambios))

    def url_revision(self, folio, numero, accion=""):
        nombres = {"": "api_guardar_revision", "emitir": "api_emitir_revision", "nueva": "api_nueva_revision"}
        return reverse(nombres[accion], args=[folio, numero])

    async def test_c1_busqueda_y_alta_con_precio_y_tipo_de_cambio_de_la_sesion(self):
        with adaptadores_registrados(adaptador("alfa", usd("7", "10.25")), adaptador("beta")), sin_red():
            busqueda = await self.async_client.get(reverse("api_buscar_productos"), {"q": "camara", "tipo_cambio": "17.50"})
        self.assertEqual(busqueda.status_code, 200)

        respuesta, cuerpo = await self.alta()
        self.assertEqual(respuesta.status_code, 201)
        datos = cuerpo["data"]
        self.assertEqual({k: datos[k] for k in ("revision", "status", "edit_version", "total")},
                         {"revision": 1, "status": "DRAFT", "edit_version": 1, "total": None})
        revision = await QuoteRevision.objects.select_related("quote").aget(quote__folio=datos["folio"])
        quote = revision.quote
        self.assertEqual((quote.advisor_id, quote.client_name, quote.client_rfc, quote.valid_days),
                         (self.asesor.pk, "Cliente sintético S.A. de C.V.", "CSI980415AA2", 15))
        self.assertEqual((revision.exchange_rate, revision.exchange_rate_source), (Decimal("17.50"), "manual"))
        self.assertEqual(revision.header["conditions"], ["Entrega en sitio.", "Pago contra entrega."])
        self.assertEqual(revision.header["company"]["name"], api.EMPRESA["name"])
        partida = await revision.snapshots.aget()
        self.assertEqual(
            (partida.provider_code, partida.provider_name, partida.external_id, partida.source_unit_price,
             partida.source_currency, partida.quantity, partida.es_mejor_precio),
            ("alfa", "Alfa", "7", Decimal("10.25"), "USD", Decimal("2"), True),
        )

    async def test_c2_el_navegador_no_puede_mandar_precio_ni_margen(self):
        for item in ({"provider": "alfa", "external_id": "7", "quantity": "1", "costo_mxn": "1.00"},
                     {"provider": "alfa", "external_id": "7", "quantity": "1", "margen": "20"}):
            with self.subTest(item=item):
                respuesta, cuerpo = await self.alta(items=[item])
                self.assertEqual(respuesta.status_code, 400)
                self.assertFalse(cuerpo["success"])
        respuesta, _ = await self.alta(tipo_cambio="1.00")
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(await Quote.objects.acount(), 0)

    async def test_c3_oferta_fuera_de_la_sesion_da_409_sin_crear_nada(self):
        respuesta, cuerpo = await self.alta(items=[{"provider": "alfa", "external_id": "nunca-visto", "quantity": "1"}])
        self.assertEqual(respuesta.status_code, 409)
        self.assertIn("Vuelve a buscar", cuerpo["error"])
        self.assertEqual(await Quote.objects.acount(), 0)

    async def test_c4_no_se_mezclan_tipos_de_cambio(self):
        respuesta, cuerpo = await self.alta(items=[
            {"provider": "alfa", "external_id": "7", "quantity": "1"},
            {"provider": "beta", "external_id": "10", "quantity": "1"},
        ])
        self.assertEqual(respuesta.status_code, 409)
        self.assertIn("tipo de cambio", cuerpo["error"])
        # MXN no depende del tipo de cambio: se combina con cualquiera.
        respuesta, _ = await self.alta(items=[
            {"provider": "alfa", "external_id": "7", "quantity": "1"},
            {"provider": "beta", "external_id": "9", "quantity": "1"},
        ])
        self.assertEqual(respuesta.status_code, 201)

    async def test_c4c_rfc_y_correo_son_opcionales(self):
        respuesta, cuerpo = await self.alta(client_rfc="", client_email="")
        self.assertEqual(respuesta.status_code, 201)
        revision = await QuoteRevision.objects.aget(quote__folio=cuerpo["data"]["folio"])
        self.assertEqual((revision.header["client"]["rfc"], revision.header["client"]["email"]), ("", ""))

    async def test_c4b_solo_mxn_no_guarda_tipo_de_cambio(self):
        respuesta, cuerpo = await self.alta(items=[{"provider": "beta", "external_id": "9", "quantity": "1"}])
        self.assertEqual(respuesta.status_code, 201)
        revision = await QuoteRevision.objects.aget(quote__folio=cuerpo["data"]["folio"])
        self.assertIsNone(revision.exchange_rate)

    async def test_c5_reintento_con_el_mismo_request_id_no_duplica(self):
        datos = cuerpo_alta()
        primera, c1 = await self.enviar("post", reverse("api_crear_cotizacion"), datos)
        segunda, c2 = await self.enviar("post", reverse("api_crear_cotizacion"), datos)
        self.assertEqual((primera.status_code, segunda.status_code), (201, 201))
        self.assertEqual(c1["data"], c2["data"])
        self.assertEqual(await Quote.objects.acount(), 1)

        for cambio in ({"valid_days": 30}, {"client_name": "Otro cliente"}, {"notes": "Otra condición."}):
            with self.subTest(cambio=cambio):
                otra, _ = await self.enviar("post", reverse("api_crear_cotizacion"), {**datos, **cambio})
                self.assertEqual(otra.status_code, 409)
        self.assertEqual(await Quote.objects.acount(), 1)

    async def test_c6_validacion_de_frontera_da_400_sin_dejar_quotes(self):
        invalidos = [
            {"client_rfc": "CST-180422-9K2"},
            {"client_email": "no-es-correo"},
            {"client_name": "  "},
            {"client_name": None},
            {"valid_days": "15"},
            {"valid_days": 0},
            {"request_id": "no-uuid"},
            {"items": []},
            {"items": ["alfa:7"]},
            {"items": [{"provider": "alfa", "external_id": "7", "quantity": 2}]},
            {"items": [{"provider": "alfa", "external_id": "7", "quantity": "dos"}]},
            {"items": [{"provider": "alfa", "external_id": "7", "quantity": "1.234"}]},
            {"items": [{"provider": "alfa", "external_id": "7", "quantity": "0"}]},
        ]
        for cambios in invalidos:
            with self.subTest(cambios=cambios):
                respuesta, cuerpo = await self.alta(**cambios)
                self.assertEqual(respuesta.status_code, 400)
                self.assertTrue(cuerpo["error"])
        for crudo in (b"{no json", b"[]"):
            respuesta = await self.async_client.post(reverse("api_crear_cotizacion"), data=crudo,
                                                     content_type="application/json")
            self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(await Quote.objects.acount(), 0)

    async def test_c7_sin_sesion_401_y_csrf_obligatorio(self):
        anonimo = AsyncClient()
        urls = [("post", reverse("api_crear_cotizacion")), ("get", reverse("api_detalle_cotizacion", args=["X"])),
                ("put", self.url_revision("X", 1)), ("post", self.url_revision("X", 1, "emitir")),
                ("post", self.url_revision("X", 1, "nueva"))]
        for metodo, url in urls:
            with self.subTest(url=url):
                respuesta = await getattr(anonimo, metodo)(url)
                self.assertEqual(respuesta.status_code, 401)

        estricto = AsyncClient(enforce_csrf_checks=True)
        await estricto.aforce_login(self.asesor)
        # El rechazo lo da CsrfViewMiddleware antes de la vista (su página es HTML, no JSON).
        respuesta = await estricto.post(reverse("api_crear_cotizacion"), data=json.dumps(cuerpo_alta()),
                                        content_type="application/json")
        self.assertEqual(respuesta.status_code, 403)
        self.assertEqual(await Quote.objects.acount(), 0)

    async def test_c8_detalle_solo_para_el_propietario_o_staff(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        url = reverse("api_detalle_cotizacion", args=[folio])

        respuesta = await self.async_client.get(url)
        self.assertEqual(respuesta.status_code, 200)
        datos = json.loads(respuesta.content)["data"]
        self.assertEqual([r["revision"] for r in datos["revisiones"]], [1])
        self.assertEqual(datos["actual"]["items"][0]["price_original"], "10.250000")
        self.assertEqual(datos["actual"]["items"][0]["quantity"], "2.00")
        self.assertEqual(datos["actual"]["tipo_cambio"]["valor"], "17.50000000")
        self.assertEqual(datos["actual"]["client"]["business_name"], "Cliente sintético S.A. de C.V.")

        otro = AsyncClient()
        await otro.aforce_login(self.otro)
        self.assertEqual((await otro.get(url)).status_code, 403)
        staff = AsyncClient()
        await staff.aforce_login(self.staff)
        self.assertEqual((await staff.get(url)).status_code, 200)
        self.assertEqual((await self.async_client.get(reverse("api_detalle_cotizacion", args=["COT-0"]))).status_code, 404)

    async def test_c8b_quote_legacy_sin_revisiones(self):
        quote = await Quote.objects.acreate(advisor=self.asesor, client_name="Legacy")
        respuesta = await self.async_client.get(reverse("api_detalle_cotizacion", args=[quote.folio]))
        datos = json.loads(respuesta.content)["data"]
        self.assertEqual((datos["revisiones"], datos["actual"]), ([], None))

    async def test_c9_edicion_completa_con_version_optimista(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        edicion = {k: v for k, v in cuerpo_alta(client_name="Cliente renombrado", valid_days=30).items()
                   if k != "request_id"}
        respuesta, cuerpo = await self.enviar("put", self.url_revision(folio, 1), {**edicion, "edit_version": 1})
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(cuerpo["data"]["edit_version"], 2)
        revision = await QuoteRevision.objects.aget(quote__folio=folio, number=1)
        self.assertEqual((revision.header["client"]["business_name"], revision.valid_days), ("Cliente renombrado", 30))

        obsoleta, cuerpo = await self.enviar("put", self.url_revision(folio, 1), {**edicion, "edit_version": 1})
        self.assertEqual(obsoleta.status_code, 409)
        sin_version, _ = await self.enviar("put", self.url_revision(folio, 1), edicion)
        self.assertEqual(sin_version.status_code, 400)
        no_existe, _ = await self.enviar("put", self.url_revision(folio, 7), {**edicion, "edit_version": 1})
        self.assertEqual(no_existe.status_code, 404)

    async def test_c10_emision_y_reemision_idempotente(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        respuesta, cuerpo = await self.enviar("post", self.url_revision(folio, 1, "emitir"), {"edit_version": 1})
        self.assertEqual(respuesta.status_code, 200)
        datos = cuerpo["data"]
        self.assertEqual(datos["status"], "ISSUED")
        # 10.25 USD × 17.50 × 2 = 358.75; IVA 16 % = 57.40.
        self.assertEqual((datos["subtotal"], datos["tax"], datos["total"]), ("358.75", "57.40", "416.15"))
        self.assertEqual(len(list(self.pdfs.rglob("*.pdf"))), 1)

        otra_vez, cuerpo = await self.enviar("post", self.url_revision(folio, 1, "emitir"), {"edit_version": 1})
        self.assertEqual((otra_vez.status_code, cuerpo["data"]), (200, datos))
        self.assertEqual(len(list(self.pdfs.rglob("*.pdf"))), 1)

        edicion = {k: v for k, v in cuerpo_alta().items() if k != "request_id"}
        emitida, _ = await self.enviar("put", self.url_revision(folio, 1), {**edicion, "edit_version": 1})
        self.assertEqual(emitida.status_code, 409)

    async def test_c11_nueva_revision_desde_una_emision(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        borrador, _ = await self.enviar("post", self.url_revision(folio, 1, "nueva"), {"request_id": str(uuid4())})
        self.assertEqual(borrador.status_code, 409)  # solo desde una emisión

        await self.enviar("post", self.url_revision(folio, 1, "emitir"), {"edit_version": 1})
        clave = str(uuid4())
        respuesta, cuerpo = await self.enviar("post", self.url_revision(folio, 1, "nueva"), {"request_id": clave})
        self.assertEqual(respuesta.status_code, 201)
        self.assertEqual({k: cuerpo["data"][k] for k in ("revision", "status", "edit_version")},
                         {"revision": 2, "status": "DRAFT", "edit_version": 1})
        repetida, otra = await self.enviar("post", self.url_revision(folio, 1, "nueva"), {"request_id": clave})
        self.assertEqual((repetida.status_code, otra["data"]["revision"]), (201, 2))

    async def test_c12_metodos_no_permitidos(self):
        self.assertEqual((await self.async_client.get(reverse("api_crear_cotizacion"))).status_code, 405)
        self.assertEqual((await self.async_client.delete(self.url_revision("X", 1))).status_code, 405)

    async def test_c13_errores_inesperados_dan_500_generico_y_quedan_en_el_log(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        with mock.patch.object(api, "aemitir_revision", side_effect=RuntimeError("detalle interno")), \
                self.assertLogs("apps.quotes.api", "ERROR"):
            respuesta, cuerpo = await self.enviar("post", self.url_revision(folio, 1, "emitir"), {"edit_version": 1})
        self.assertEqual(respuesta.status_code, 500)
        self.assertNotIn("detalle interno", cuerpo["error"])

    async def test_c13b_un_type_error_de_m8_es_400_generico(self):
        _, alta = await self.alta()
        with mock.patch.object(api, "aemitir_revision", side_effect=TypeError("Se requiere X")):
            respuesta, cuerpo = await self.enviar(
                "post", self.url_revision(alta["data"]["folio"], 1, "emitir"), {"edit_version": 1}
            )
        self.assertEqual((respuesta.status_code, cuerpo["error"]), (400, "Datos de solicitud inválidos."))

    async def test_c14_original_dañado_da_503(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        with mock.patch.object(api, "aemitir_revision", side_effect=ArchivedPDFError("x")):
            respuesta, _ = await self.enviar("post", self.url_revision(folio, 1, "emitir"), {"edit_version": 1})
        self.assertEqual(respuesta.status_code, 503)

    async def test_c15_choque_de_folio_se_reintenta(self):
        _, alta = await self.alta()
        repetido = alta["data"]["folio"]
        with mock.patch.object(Quote, "_generate_folio", side_effect=[repetido, "COT-2099-0001"]):
            respuesta, cuerpo = await self.alta()
        self.assertEqual((respuesta.status_code, cuerpo["data"]["folio"]), (201, "COT-2099-0001"))

        with mock.patch.object(Quote, "_generate_folio", return_value=repetido), \
                self.assertLogs("apps.quotes.api", "ERROR"):
            respuesta, _ = await self.alta()
        self.assertEqual(respuesta.status_code, 500)
        self.assertEqual(await Quote.objects.acount(), 2)



@tag("django_db", "concurrencia")
class AltaConcurrenteTests(TransactionTestCase):
    """C16: dos altas simultáneas con el mismo request_id (doble clic) dejan una sola Quote."""

    def test_c16_el_bloqueo_por_usuario_evita_quotes_duplicadas(self):
        tmp = tempfile.TemporaryDirectory(prefix="m7a-")
        self.addCleanup(tmp.cleanup)
        self.enterContext(override_settings(QUOTE_PDF_ROOT=tmp.name))
        asesor = crear_asesor()
        partidas, tipo = api._partidas(
            [{"provider": "alfa", "external_id": "7", "quantity": "1"}],
            {"alfa:7": oferta_guardada("alfa", "7", "10.25")},
        )
        cliente = api._cliente(cuerpo_alta())
        request_id = uuid4()

        async def alta():
            return await sync_to_async(api._crear_cotizacion, thread_sensitive=True)(
                asesor, cliente, partidas, tipo, request_id,
            )

        # Sin bloqueo, los dos hilos pasarían la búsqueda de request_id antes de que el otro
        # cree su Quote. La barrera los junta justo antes de crearla para forzar ese choque;
        # con el bloqueo el segundo espera, la barrera caduca y encuentra la revisión del primero.
        original = Quote.full_clean
        barrera = threading.Barrier(2)

        def full_clean_sincronizado(quote, *args, **kwargs):
            try:
                barrera.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass
            return original(quote, *args, **kwargs)

        # Folios distintos: dos altas con el mismo folio ya chocan en su unique y el reintento
        # las une por casualidad; aquí la idempotencia no puede depender de eso.
        folios = iter(["COT-2099-0001", "COT-2099-0002"])
        with mock.patch.object(Quote, "full_clean", full_clean_sincronizado),                 mock.patch.object(Quote, "_generate_folio", lambda quote: next(folios)):
            resultados = ejecutar_en_hilos(alta, [(), ()])
        self.assertEqual(resultados[0], resultados[1])
        self.assertEqual(Quote.objects.count(), 1)
