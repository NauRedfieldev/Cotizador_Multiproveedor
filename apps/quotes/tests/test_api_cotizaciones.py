"""Pruebas C1–C21 de la API de cotizaciones de M7a sobre M8 (docs/28), sin red.

Datos sintéticos (también la empresa: nunca los de pdf.COMPANY) y PDF en un directorio temporal, como test_snapshots. Las ofertas se ponen
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
from django.core.exceptions import ValidationError
from django.contrib.auth.models import Permission
from asgiref.sync import sync_to_async
from django.test import AsyncClient, TestCase, TransactionTestCase, override_settings, tag
from django.urls import reverse

from apps.providers.tests.soporte import adaptadores_registrados, crear_proveedor, ejecutar_en_hilos, sin_red
from apps.quotes import api
from apps.quotes.models import Quote, QuoteRevision
from apps.quotes.snapshots import ArchivedPDFError
from apps.quotes.tests.test_vistas_cotizacion import adaptador, crear_asesor, usd
from apps.quotes.tests.test_commercial_persistence import CommercialFixtures
from apps.quotes.models import QuoteAPIRequest, QuoteSelectionContext
from apps.quotes.permissions import FX, MANUAL_SALE, MARGIN, set_commercial_permission

TIPO = {"valor": "17.50", "origen": "manual", "fecha": "2026-10-15T18:00:00+00:00"}
EMPRESA_QA = {"name": "Empresa sintética (QA)", "rfc": "", "tagline": "Datos sintéticos de prueba",
              "phone": "", "email": ""}
CONDICIONES = ["Entrega en sitio.", "Pago contra entrega."]


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
        "conditions": list(CONDICIONES),
        "notes": "Nota interna: no se publica.",
        "items": [{"provider": "alfa", "external_id": "7", "quantity": "2"}],
    }
    datos.update(cambios)
    return datos


def cuerpo_edicion(edit_version=1, **cambios):
    """Reemplazo completo: lo mismo que el alta, sin request_id ni notes (nota interna del alta)."""
    datos = {k: v for k, v in cuerpo_alta(**cambios).items() if k not in ("request_id", "notes")}
    return {**datos, "edit_version": edit_version}


# La emisión se habilita aquí para probar su flujo; C18 prueba el bloqueo con el valor real.
@override_settings(CCONOR_EMPRESA=EMPRESA_QA, COTIZACIONES_EMISION_HABILITADA=True)


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
        view = Permission.objects.get(content_type__app_label="quotes", codename="view_commercial_costs")
        for actor in (self.asesor, self.otro, self.staff):
            actor.user_permissions.add(view)
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
        # Condiciones tal cual llegaron; la nota interna se queda en la Quote y no entra en la cabecera.
        self.assertEqual(revision.header["conditions"], CONDICIONES)
        self.assertEqual(quote.notes, "Nota interna: no se publica.")
        self.assertNotIn("Nota interna", json.dumps(revision.header, ensure_ascii=False))
        self.assertEqual(revision.header["company"]["name"], "Empresa sintética (QA)")
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

        for cambio in ({"valid_days": 30}, {"client_name": "Otro cliente"}, {"conditions": ["Otra."]},
                       {"notes": "Otra nota interna."}):
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
            {"conditions": "Entrega en sitio."},
            {"conditions": [1]},
            {"conditions": ["  "]},
            {"conditions": None},
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
        edicion = cuerpo_edicion(client_name="Cliente renombrado", valid_days=30, conditions=[])
        del edicion["edit_version"]  # cada envío pone la suya; sin ella debe dar 400
        respuesta, cuerpo = await self.enviar("put", self.url_revision(folio, 1), {**edicion, "edit_version": 1})
        self.assertEqual(respuesta.status_code, 409)  # P4: transición explícita, no recaptura v1.
        revision = await QuoteRevision.objects.aget(quote__folio=folio, number=1)
        self.assertEqual((revision.header["client"]["business_name"], revision.valid_days), ("Cliente sintético S.A. de C.V.", 15))
        self.assertEqual(revision.header["conditions"], CONDICIONES)
        self.assertEqual(revision.header["company"]["name"], "Empresa sintética (QA)")

        # notes es del alta: en la edición se rechaza en lugar de ignorarla.
        con_notas, _ = await self.enviar("put", self.url_revision(folio, 1),
                                         {**edicion, "edit_version": 2, "notes": "x"})
        self.assertEqual(con_notas.status_code, 400)

        obsoleta, cuerpo = await self.enviar("put", self.url_revision(folio, 1), {**edicion, "edit_version": 1})
        self.assertEqual(obsoleta.status_code, 409)
        sin_version, _ = await self.enviar("put", self.url_revision(folio, 1), edicion)
        self.assertEqual(sin_version.status_code, 409)  # primero se informa incompatibilidad de esquema
        no_existe, _ = await self.enviar("put", self.url_revision(folio, 7), {**edicion, "edit_version": 1})
        self.assertEqual(no_existe.status_code, 404)

    async def test_c10_emision_y_reemision_idempotente(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        respuesta, cuerpo = await self.enviar("post", self.url_revision(folio, 1, "emitir"), {"edit_version": 1})
        self.assertEqual(respuesta.status_code, 409)  # nunca emisión HTTP a costo v1
        self.assertEqual(len(list(self.pdfs.rglob("*.pdf"))), 0)

        otra_vez, cuerpo = await self.enviar("post", self.url_revision(folio, 1, "emitir"), {"edit_version": 1})
        self.assertEqual(otra_vez.status_code, 409)
        self.assertEqual(len(list(self.pdfs.rglob("*.pdf"))), 0)

        emitida, _ = await self.enviar("put", self.url_revision(folio, 1), cuerpo_edicion())
        self.assertEqual(emitida.status_code, 409)

    async def test_c11_nueva_revision_desde_una_emision(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        borrador, _ = await self.enviar("post", self.url_revision(folio, 1, "nueva"), {"request_id": str(uuid4())})
        self.assertEqual(borrador.status_code, 409)  # solo desde una emisión

        # Original v1 histórico creado por servicio interno, no por emisión HTTP nueva.
        from apps.quotes.snapshots import emitir_revision
        revision = await QuoteRevision.objects.aget(quote__folio=folio)
        await sync_to_async(emitir_revision, thread_sensitive=True)(revision.pk, actor=self.asesor, expected_version=1)
        clave = str(uuid4())
        respuesta, cuerpo = await self.enviar("post", self.url_revision(folio, 1, "nueva"), {"request_id": clave})
        self.assertEqual(respuesta.status_code, 201)
        self.assertEqual({k: cuerpo["data"][k] for k in ("revision", "status", "edit_version")},
                         {"revision": 2, "status": "DRAFT", "edit_version": 1})
        repetida, otra = await self.enviar("post", self.url_revision(folio, 1, "nueva"), {"request_id": clave})
        self.assertEqual((repetida.status_code, otra["data"]["revision"]), (201, 2))

    async def test_c12_metodos_no_permitidos(self):
        self.assertEqual((await self.async_client.patch(reverse("api_crear_cotizacion"))).status_code, 405)
        self.assertEqual((await self.async_client.delete(self.url_revision("X", 1))).status_code, 405)

    async def test_c13_errores_inesperados_dan_500_generico_y_quedan_en_el_log(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        with mock.patch.object(api, "_commercial_edit", side_effect=RuntimeError("detalle interno")), \
                self.assertLogs("apps.quotes.api", "ERROR"):
            respuesta, cuerpo = await self.enviar("put", self.url_revision(folio, 1), cuerpo_edicion())
        self.assertEqual(respuesta.status_code, 500)
        self.assertNotIn("detalle interno", cuerpo["error"])

    async def test_c13b_un_type_error_de_m8_es_400_generico(self):
        _, alta = await self.alta()
        with mock.patch.object(api, "_commercial_edit", side_effect=TypeError("Se requiere X")):
            respuesta, cuerpo = await self.enviar(
                "put", self.url_revision(alta["data"]["folio"], 1), cuerpo_edicion()
            )
        self.assertEqual((respuesta.status_code, cuerpo["error"]), (400, "Datos de solicitud inválidos."))

    async def test_c14_original_dañado_da_503(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        respuesta = api._respuesta_de_error(ArchivedPDFError("x"))
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


    async def test_c17_sin_empresa_configurada_no_se_crea_nada(self):
        for empresa in ({**EMPRESA_QA, "name": ""}, {**EMPRESA_QA, "name": "   "}):
            with self.subTest(empresa=empresa), override_settings(CCONOR_EMPRESA=empresa):
                respuesta, cuerpo = await self.alta()
                self.assertEqual(respuesta.status_code, 503)
                self.assertIn("empresa sin configurar", cuerpo["error"])
        self.assertEqual(await Quote.objects.acount(), 0)

    async def test_c18_la_emision_esta_bloqueada_sin_politica_de_precios(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        with override_settings(COTIZACIONES_EMISION_HABILITADA=False):
            respuesta, cuerpo = await self.enviar("post", self.url_revision(folio, 1, "emitir"), {"edit_version": 1})
        self.assertEqual(respuesta.status_code, 403)
        self.assertIn("deshabilitada", cuerpo["error"])
        self.assertEqual((await QuoteRevision.objects.aget(quote__folio=folio)).status, "DRAFT")
        self.assertEqual(list(self.pdfs.rglob("*.pdf")), [])

    async def test_c19_condiciones_obligatorias_aunque_vacias(self):
        sin_condiciones = {k: v for k, v in cuerpo_alta().items() if k != "conditions"}
        respuesta, _ = await self.enviar("post", reverse("api_crear_cotizacion"), sin_condiciones)
        self.assertEqual(respuesta.status_code, 400)
        respuesta, cuerpo = await self.alta(conditions=[])
        self.assertEqual(respuesta.status_code, 201)
        revision = await QuoteRevision.objects.aget(quote__folio=cuerpo["data"]["folio"])
        self.assertEqual(revision.header["conditions"], [])

    async def test_c20_la_edicion_conserva_la_empresa_capturada(self):
        _, alta = await self.alta()
        folio = alta["data"]["folio"]
        with override_settings(CCONOR_EMPRESA={**EMPRESA_QA, "name": "Otra empresa"}):
            respuesta, _ = await self.enviar("put", self.url_revision(folio, 1), cuerpo_edicion())
        self.assertEqual(respuesta.status_code, 409)
        revision = await QuoteRevision.objects.aget(quote__folio=folio)
        self.assertEqual(revision.header["company"]["name"], "Empresa sintética (QA)")

    async def test_c21_el_detalle_no_expone_la_nota_interna(self):
        _, alta = await self.alta()
        respuesta = await self.async_client.get(reverse("api_detalle_cotizacion", args=[alta["data"]["folio"]]))
        self.assertNotIn("Nota interna", respuesta.content.decode())
        self.assertEqual(json.loads(respuesta.content)["data"]["actual"]["conditions"], CONDICIONES)


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
                asesor, EMPRESA_QA, cliente, partidas, tipo, request_id,
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


def partida_api_manual(**changes):
    data = {"line_key": str(uuid4()), "source": {"type": "MANUAL", "name": "Servicio sintético"},
        "cost": None, "quantity": 1,
        "units": {"source_label": "Servicio", "sale_label": "Servicio", "conversion_factor": "1", "confirmed": True},
        "pricing": {"mode": "MANUAL", "sale": {"amount": "100.00", "currency": "MXN", "vat_basis": "NET"}}}
    data.update(changes)
    return data


def alta_comercial(**changes):
    data = {**cuerpo_alta(), "schema_version": 2, "items": [partida_api_manual()],
        "tax_rate": "0.16", "discount_percent": "0", "fx_context_id": None, "issue_date": "2026-10-16"}
    data.update(changes)
    return data


@override_settings(CCONOR_EMPRESA=EMPRESA_QA, COTIZACIONES_EMISION_HABILITADA=True)
class CommercialAPITests(CommercialFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.actor)
        self.enterContext(override_settings(MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}))

    def send(self, name, data=None, *, method="post", args=(), status=200):
        url = reverse(name, args=args)
        response = getattr(self.client, method)(url, data=json.dumps(data) if data is not None else None,
                                               content_type="application/json")
        self.assertEqual(response.status_code, status, response.content.decode()[:1000])
        return response.json().get("data", response.json())

    def create(self, data=None):
        return self.send("api_crear_cotizacion", data or alta_comercial(), status=201)

    def detail(self, created):
        return self.send("api_guardar_revision", method="get", args=(created["folio"], created["revision"]))

    def confirm_and_issue(self, created):
        detail = self.detail(created)
        warnings = [w for w in detail["preview"]["warnings"] if w["requires_confirmation"]]
        self.send("api_confirmar_comercial", {"edit_version": created["edit_version"],
            "state_fingerprint": created["state_fingerprint"], "warnings": [
                {"code": w["code"], "line_key": w["line_key"]} for w in warnings]},
            args=(created["folio"], created["revision"]))
        return self.send("api_emitir_revision", {"edit_version": created["edit_version"],
            "state_fingerprint": created["state_fingerprint"]}, args=(created["folio"], created["revision"]))

    def test_preview_antes_de_quote_no_escribe_ni_reserva_folio(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        data = {k: v for k, v in alta_comercial().items() if k not in ("notes", "request_id")}
        before = Quote.objects.count()
        with CaptureQueriesContext(connection) as queries:
            result = self.send("api_preview_nueva", data)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertFalse(result["persistida"])
        self.assertIsNone(result["cliente"]["folio"])
        self.assertIsNone(result["cliente"]["revision"])
        self.assertEqual(result["cliente"]["total"], "116.00")
        self.assertEqual(Quote.objects.count(), before)
        self.assertFalse(QuoteAPIRequest.objects.exists())
        self.assertFalse(self.files())

    def test_alta_detalle_exactos_y_recibo_sin_notas_en_salida(self):
        data = alta_comercial()
        created = self.create(data)
        detail = self.detail(created)
        self.assertEqual((detail["schema_version"], detail["calculation_version"]), (2, 1))
        self.assertEqual(detail["items"][0]["line_key"], data["items"][0]["line_key"])
        self.assertIsNone(detail["items"][0]["source_unit_price"])
        self.assertEqual(detail["preview"]["resultado"]["total"], "116.00")
        self.assertNotIn("Nota interna", json.dumps(detail))
        self.assertEqual(QuoteAPIRequest.objects.count(), 1)

    def test_reintentar_no_lee_empresa_capturas_o_defaults_vivos(self):
        data = alta_comercial()
        first = self.create(data)
        with override_settings(CCONOR_EMPRESA={}), mock.patch.object(api, "_commercial_items", side_effect=AssertionError("No recapturar")):
            again = self.create(data)
        self.assertEqual(again["folio"], first["folio"])
        self.assertTrue(again["replayed"])
        self.assertEqual(QuoteAPIRequest.objects.count(), 1)
        self.send("api_crear_cotizacion", {**data, "notes": "Otra intención"}, status=409)

    def test_recibo_reintento_tras_editar_devuelve_estado_vigente(self):
        data = alta_comercial()
        first = self.create(data)
        edited = {k: v for k, v in data.items() if k not in ("schema_version", "request_id", "notes")}
        edited["items"][0]["source"] = {"type": "EXISTING"}
        # No mutar intención original: restaurar una copia independiente para reintento.
        original = alta_comercial(request_id=data["request_id"], items=[partida_api_manual(line_key=data["items"][0]["line_key"])])
        edited.update(edit_version=1, client_name="Cliente editado QA")
        self.send("api_guardar_revision", edited, method="put", args=(first["folio"], 1))
        replay = self.create(original)
        self.assertEqual(replay["edit_version"], 2)
        self.assertTrue(replay["replayed"])

    def test_fallo_no_deja_quote_o_recibo(self):
        before = Quote.objects.count()
        self.send("api_crear_cotizacion", alta_comercial(items=[partida_api_manual(quantity=0)]), status=400)
        self.assertEqual(Quote.objects.count(), before)
        self.assertFalse(QuoteAPIRequest.objects.exists())

    def test_decimal_numerico_exponente_y_cantidad_fraccion_no_admitidos(self):
        for price in (100.0, "1e2", "NaN", "-1", "1,000"):
            line = partida_api_manual()
            line["pricing"]["sale"]["amount"] = price
            self.send("api_crear_cotizacion", alta_comercial(items=[line]), status=400)
        for quantity in (True, "1", 1.5):
            self.send("api_crear_cotizacion", alta_comercial(items=[partida_api_manual(quantity=quantity)]), status=400)

    def test_confirmaciones_reales_y_emision_archivada_sin_recalculo(self):
        created = self.create()
        self.send("api_emitir_revision", {"edit_version": 1, "state_fingerprint": created["state_fingerprint"]},
            args=(created["folio"], 1), status=403)
        issued = self.confirm_and_issue(created)
        self.assertEqual((issued["total"], issued["status"]), ("116.00", "ISSUED"))
        from apps.quotes import snapshots
        with mock.patch.object(snapshots, "_commercial_calculate", side_effect=AssertionError("No recalcular")):
            detail = self.detail(created)
            self.assertEqual(detail["sealed"]["gross_profit"], None)
            self.assertTrue(detail["cliente"]["archived_only"])
            self.send("api_emitir_revision", {"edit_version": 1, "state_fingerprint": created["state_fingerprint"]}, args=(created["folio"], 1))
        self.assertEqual(len(self.files()), 1)

    def test_huella_incorrecta_y_actor_falso_se_rechazan(self):
        created = self.create()
        self.send("api_emitir_revision", {"edit_version": 1, "state_fingerprint": "0" * 64}, args=(created["folio"], 1), status=409)
        self.send("api_confirmar_comercial", {"edit_version": 1, "state_fingerprint": created["state_fingerprint"],
            "warnings": [], "confirmed_by": self.root.pk}, args=(created["folio"], 1), status=400)

    def test_edicion_preview_sin_sesion_y_cambio_cliente_invalida_avisos(self):
        data = alta_comercial()
        created = self.create(data)
        edit = {k: v for k, v in data.items() if k not in ("schema_version", "request_id", "notes")}
        edit["items"][0]["source"] = {"type": "EXISTING"}
        edit["items"][0]["quantity"] = 2
        edit.update(edit_version=1, client_name="Cambio de cliente QA")
        preview = self.send("api_preview_edicion", edit, args=(created["folio"], 1))
        self.assertEqual(preview["cliente"]["total"], "232.00")
        self.assertEqual(self.detail(created)["edit_version"], 1)
        saved = self.send("api_guardar_revision", edit, method="put", args=(created["folio"], 1))
        self.assertEqual(saved["state_fingerprint"], preview["state_fingerprint"])
        self.send("api_guardar_revision", edit, method="put", args=(created["folio"], 1), status=409)

    def test_reordenar_partidas_guarda_el_nuevo_orden(self):
        # 7A: arrastrar partidas en el cotizador solo cambia el orden de items al guardar.
        first, second = partida_api_manual(source={"type": "MANUAL", "name": "Primera"}), partida_api_manual(source={"type": "MANUAL", "name": "Segunda"})
        data = alta_comercial(items=[first, second])
        created = self.create(data)
        self.assertEqual([i["name"] for i in self.detail(created)["items"]], ["Primera", "Segunda"])
        edit = {k: v for k, v in data.items() if k not in ("schema_version", "request_id", "notes")}
        edit["items"] = [{**line, "source": {"type": "EXISTING"}} for line in (second, first)]
        edit.update(edit_version=1)
        saved = self.send("api_guardar_revision", edit, method="put", args=(created["folio"], 1))
        self.assertNotEqual(saved["state_fingerprint"], created["state_fingerprint"])
        detail = self.detail(created)
        self.assertEqual([i["name"] for i in detail["items"]], ["Segunda", "Primera"])
        self.assertEqual([i["line_key"] for i in detail["items"]], [second["line_key"], first["line_key"]])

    def test_otro_asesor_y_staff_no_financiero_no_acceden(self):
        created = self.create()
        self.client.force_login(self.other)
        self.send("api_guardar_revision", method="get", args=(created["folio"], 1), status=403)
        self.staff.groups.remove(self.internal)
        self.client.force_login(self.staff)
        self.send("api_guardar_revision", method="get", args=(created["folio"], 1), status=403)

    def test_csrf_sesion_y_metodos(self):
        from django.test import Client
        anonymous = self.client_class()
        self.assertEqual(anonymous.post(reverse("api_preview_nueva")).status_code, 401)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.actor)
        self.assertEqual(strict.post(reverse("api_preview_nueva"), data="{}", content_type="application/json").status_code, 403)
        self.assertEqual(self.client.get(reverse("api_preview_nueva")).status_code, 405)

    def test_fx_recibo_y_capacidad_separada(self):
        data = {"request_id": str(uuid4()), "value": "17.50"}
        first = self.send("api_contexto_fx", data, status=201)
        again = self.send("api_contexto_fx", data, status=201)
        self.assertEqual(first["fx_context_id"], again["fx_context_id"])
        self.send("api_contexto_fx", {**data, "value": "18"}, status=409)
        self.send("api_crear_cotizacion", alta_comercial(request_id=data["request_id"]), status=409)
        self.client.force_login(self.other)
        self.send("api_contexto_fx", {"request_id": str(uuid4()), "value": "17.50"}, status=403)

    def test_contextos_y_recibos_son_inmutables(self):
        result = self.send("api_contexto_fx", {"request_id": str(uuid4()), "value": "17.5"}, status=201)
        context = QuoteSelectionContext.objects.get(pk=result["fx_context_id"])
        from django.core.exceptions import ValidationError
        for action in (context.save, context.delete, lambda: QuoteSelectionContext.objects.all().update(payload={}),
                       QuoteAPIRequest.objects.first().save, QuoteAPIRequest.objects.first().delete):
            with self.assertRaises(ValidationError):
                action()

    def test_gestor_limita_permisos_y_no_cambia_privilegios_tecnicos(self):
        self.send("api_permisos_comerciales", {"capability": MARGIN, "enabled": True}, method="patch", args=(self.other.pk,))
        self.other.refresh_from_db()
        self.assertFalse(self.other.is_staff)
        self.assertFalse(self.other.is_superuser)
        self.send("api_permisos_comerciales", {"capability": "manage_commercial_permissions", "enabled": True},
            method="patch", args=(self.other.pk,), status=400)
        self.client.force_login(self.other)
        self.send("api_permisos_comerciales", method="get", args=(self.actor.pk,), status=403)
        result = self.send("api_capacidades_comerciales", method="get")
        self.assertTrue(result[MARGIN])

    def test_clone_y_transicion_explicita_preservan_origen(self):
        original = self.draft()
        line = partida_api_manual()
        data = {"schema_version": 2, "request_id": str(uuid4()), "items": [line], "tax_rate": ".16",
                "discount_percent": "0", "fx_context_id": None}
        # El protocolo requiere cero inicial.
        data["tax_rate"] = "0.16"
        created = self.send("api_nueva_revision", data, args=(self.quote.folio, original.number), status=201)
        original.refresh_from_db()
        self.assertEqual(original.schema_version, 1)
        issued = self.confirm_and_issue(created)
        key = {"schema_version": 2, "request_id": str(uuid4())}
        clone = self.send("api_nueva_revision", key, args=(created["folio"], created["revision"]), status=201)
        with mock.patch.object(api, "_commercial_items", side_effect=AssertionError("No recapturar")):
            replay = self.send("api_nueva_revision", key, args=(created["folio"], created["revision"]), status=201)
        self.assertEqual(clone["revision"], replay["revision"])
        detail = self.detail(clone)
        self.assertEqual(detail["confirmations"], [])
        self.assertEqual(detail["items"][0]["line_key"], line["line_key"])

    def test_emision_sigue_bloqueada_por_bandera(self):
        created = self.create()
        with override_settings(COTIZACIONES_EMISION_HABILITADA=False):
            self.send("api_emitir_revision", {"edit_version": 1, "state_fingerprint": created["state_fingerprint"]},
                args=(created["folio"], 1), status=403)
        self.assertFalse(self.files())

    def test_iva_incluido_manual_independiente_iva_de_venta(self):
        line = partida_api_manual()
        line["pricing"]["sale"].update(amount="116", vat_basis="INCLUDED", vat_rate="0.16")
        created = self.create(alta_comercial(items=[line], tax_rate="0.08"))
        detail = self.detail(created)
        self.assertEqual(detail["items"][0]["manual_unit_price"], "116.000000")
        self.assertEqual(detail["items"][0]["manual_tax_rate_included"], "0.1600")
        self.assertEqual(detail["preview"]["resultado"]["subtotal_neto"], "100.00")
        self.assertEqual(detail["preview"]["cliente"]["total"], "108.00")

    def test_perdida_total_cero_y_costo_cero_conocido(self):
        line = partida_api_manual(cost={"amount": "100", "currency": "MXN", "vat_basis": "NET"},
                                  pricing={"mode": "MARGIN", "margin_percent": "35"})
        created = self.create(alta_comercial(items=[line], discount_percent="100"))
        warnings = self.detail(created)["preview"]["warnings"]
        self.assertEqual({w["code"] for w in warnings}, {"VENTA_BAJO_COSTO", "TOTAL_CERO"})
        issued = self.confirm_and_issue(created)
        self.assertEqual(issued["total"], "0.00")
        self.assertEqual(self.detail(created)["sealed"]["gross_profit"], "-100.00")
        zero = partida_api_manual(cost={"amount": "0", "currency": "MXN", "vat_basis": "NET"})
        other = self.create(alta_comercial(items=[zero]))
        self.assertEqual(self.detail(other)["preview"]["warnings"], [])

    def test_avisos_de_stock_informativos_y_confirmable_en_api(self):
        from apps.quotes.tests.test_commercial_persistence import commercial_line
        cero, desconocido = commercial_line(kind="API", stock=0), commercial_line(kind="API", stock=None, external_id="QA-2")
        r = self.commercial_draft([cero, desconocido, commercial_line()])
        created = {"folio": self.quote.folio, "revision": r.number, "edit_version": r.edit_version,
                   "state_fingerprint": r.state_fingerprint}
        warnings = {w["code"]: w for w in self.detail(created)["preview"]["warnings"]}
        self.assertEqual(set(warnings), {"STOCK_CERO", "STOCK_DESCONOCIDO"})
        self.assertEqual(warnings["STOCK_CERO"], {"code": "STOCK_CERO", "line_key": str(cero.line_key),
            "requires_confirmation": False, "required_capability": None, "confirmed": False})
        self.assertEqual((warnings["STOCK_DESCONOCIDO"]["requires_confirmation"], warnings["STOCK_DESCONOCIDO"]["required_capability"]),
                         (True, "view_commercial_costs"))
        self.send("api_confirmar_comercial", {"edit_version": r.edit_version, "state_fingerprint": r.state_fingerprint,
            "warnings": [{"code": "STOCK_CERO", "line_key": str(cero.line_key)}]}, args=(self.quote.folio, r.number), status=400)
        self.send("api_emitir_revision", {"edit_version": r.edit_version, "state_fingerprint": r.state_fingerprint},
                  args=(self.quote.folio, r.number), status=403)
        self.assertEqual(self.confirm_and_issue(created)["status"], "ISSUED")

    def test_estado_confirmacion_se_revalida_y_revocacion_no_queda_cacheada(self):
        created = self.create()
        self.assertFalse(self.detail(created)["preview"]["warnings"][0]["confirmed"])
        detail = self.detail(created)
        warning = detail["preview"]["warnings"][0]
        self.send("api_confirmar_comercial", {"edit_version": 1, "state_fingerprint": created["state_fingerprint"],
            "warnings": [{"code": warning["code"], "line_key": warning["line_key"]}]}, args=(created["folio"], 1))
        self.assertTrue(self.detail(created)["preview"]["warnings"][0]["confirmed"])
        self.actor.groups.remove(self.commercial_admin)
        self.assertFalse(self.detail(created)["preview"]["warnings"][0]["confirmed"])
        self.send("api_emitir_revision", {"edit_version": 1, "state_fingerprint": created["state_fingerprint"]},
                  args=(created["folio"], 1), status=403)

    def test_reintento_sin_permiso_interno_no_devuelve_datos(self):
        body = alta_comercial()
        self.create(body)
        self.actor.groups.clear()
        self.send("api_crear_cotizacion", body, status=403)

    def test_contexto_fx_ajeno_y_metadatos_falsos_se_rechazan(self):
        fx = self.send("api_contexto_fx", {"request_id": str(uuid4()), "value": "17.5"}, status=201)
        self.send("api_contexto_fx", {"request_id": str(uuid4()), "value": "17.5", "source": "proveedor"}, status=400)
        self.client.force_login(self.other)
        self.send("api_crear_cotizacion", alta_comercial(fx_context_id=fx["fx_context_id"]), status=409)

    def test_detalle_devuelve_evidencia_fx_capturada_para_interfaz(self):
        fx = self.send("api_contexto_fx", {"request_id": str(uuid4()), "value": "17.12345678"}, status=201)
        created = self.create(alta_comercial(fx_context_id=fx["fx_context_id"]))
        detail = self.detail(created)
        self.assertEqual(detail["tipo_cambio"]["valor"], "17.12345678")
        self.assertEqual(detail["tipo_cambio"]["origen"], "manual")
        self.assertEqual(detail["fx_context_id"], fx["fx_context_id"])
        capabilities = self.client.get(reverse("api_capacidades_comerciales")).json()
        self.assertEqual(capabilities["actor_id"], self.actor.pk)

    def test_pagina_cotizador_conectada_sin_filas_o_totales_demo(self):
        response = self.client.get(reverse("cotizador"))
        self.assertEqual(response.status_code, 200)
        for text in ("commercialQuoteApp", "quoteConditions", "quoteWarnings", "addManualBtn", "Ábrela desde Mis cotizaciones"):
            self.assertContains(response, text)
        self.assertContains(response, f'href="{reverse("resumen")}"')
        self.assertNotContains(response, "Abrir revisión exacta")  # Abrir otra revisión se hace desde Mis cotizaciones.
        for demo in ("SYS-PRO-CAT6", "CT-DELL-P2422H", "$64,335.90", 'value="17.50"', "Openpay / Stripe"):
            self.assertNotContains(response, demo)
        self.assertContains(response, "viewSummaryBtn")

    def test_json_ambiguo_no_se_acepta(self):
        for body in ('{"schema_version":2,"schema_version":2}', '{"schema_version":NaN}'):
            response = self.client.post(reverse("api_crear_cotizacion"), body, content_type="application/json")
            self.assertEqual(response.status_code, 400)

    def test_fechas_y_schema_estrictos_y_errores_estructurados(self):
        for value in ("20261016", "2026-02-30", None):
            result = self.send("api_crear_cotizacion", alta_comercial(issue_date=value), status=400)
            self.assertEqual(result["code"], "INVALID_INPUT")
            self.assertEqual(result["details"], {})
        self.send("api_crear_cotizacion", alta_comercial(schema_version=True), status=400)

    def test_recibo_no_se_sobrescribe_con_objeto_nuevo_misma_pk(self):
        self.create()
        old = QuoteAPIRequest.objects.get()
        from django.db import IntegrityError, transaction
        copy = QuoteAPIRequest(pk=old.pk, actor=old.actor, request_id=uuid4(), operation=old.operation,
                               fingerprint=old.fingerprint, revision=old.revision)
        with self.assertRaises((IntegrityError, ValidationError)), transaction.atomic():
            copy.save()
        self.assertEqual(QuoteAPIRequest.objects.get().request_id, old.request_id)


@override_settings(CCONOR_EMPRESA=EMPRESA_QA, COTIZACIONES_EMISION_HABILITADA=True)
class DocumentalAPITests(CommercialFixtures, TestCase):
    send = CommercialAPITests.send
    create = CommercialAPITests.create
    detail = CommercialAPITests.detail
    confirm_and_issue = CommercialAPITests.confirm_and_issue

    def setUp(self):
        super().setUp()
        self.client.force_login(self.actor)

    def summary(self, created):
        return self.send("api_resumen_documental", method="get", args=(created["folio"], created["revision"]))

    def document_params(self, created):
        return {"exact": "1", "revision": created["revision"], "edit_version": created["edit_version"],
                "state_fingerprint": created["state_fingerprint"], "expected_status": created.get("status", "DRAFT")}

    def test_listado_solo_propias_incluso_staff_y_sin_costos(self):
        first = self.create(alta_comercial(client_name="QA propietario"))
        other = self.create(alta_comercial(client_name="QA ajena"))
        quote = Quote.objects.get(folio=other["folio"])
        quote.advisor = self.other
        quote.save(update_fields=["advisor"])
        response = self.client.get(reverse("api_crear_cotizacion"))
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual([r["folio"] for r in result["data"]], [first["folio"]])
        self.assertIsNone(result["data"][0]["total"])
        for internal in ("source_unit_price", "provider", "state_fingerprint", "notes", "known_cost_total"):
            self.assertNotIn(internal, response.content.decode())
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("api_crear_cotizacion")).json()["data"], [])

    def test_listado_paginado_y_filtros_rechazan_entradas_ambiguas(self):
        for i in range(21):
            self.create(alta_comercial(client_name=f"QA cliente {i}"))
        url = reverse("api_crear_cotizacion")
        first = self.client.get(url).json()
        self.assertEqual(len(first["data"]), 20)
        self.assertEqual(first["pagination"], {"page": 1, "pages": 2, "count": 21})
        self.assertEqual(len(self.client.get(url, {"page": 2}).json()["data"]), 1)
        self.assertEqual(len(self.client.get(url, {"q": "QA cliente 20"}).json()["data"]), 1)
        self.assertEqual(self.client.get(url, {"status": "ISSUED"}).json()["data"], [])
        for query in ("page=0", "page=3", "page=1&page=2", "status=enviada", "advisor=1", "page=1.5"):
            self.assertEqual(self.client.get(url + "?" + query).status_code, 400)

    def test_listado_busca_cabecera_de_ultima_revision_no_cliente_antiguo(self):
        original = self.issue(self.commercial_draft())
        from apps.quotes.snapshots import crear_revision_comercial
        from apps.quotes.tests.test_commercial_persistence import commercial_line
        draft = crear_revision_comercial(original.pk, [commercial_line()], actor=self.actor, request_id=uuid4(),
            tax_rate=Decimal(".16"), discount_percent=Decimal("0"), tipo_cambio=None, exchange_context_id=None)
        self.commercial_edit(draft, client={**draft.header["client"], "business_name": "Cliente nuevo revisado"})
        result = self.client.get(reverse("api_crear_cotizacion"), {"q": "nuevo revisado"}).json()["data"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["revision"], 2)
        self.assertIsNone(result[0]["total"])

    def test_listado_sesion_y_revocacion(self):
        url = reverse("api_crear_cotizacion")
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 401)
        self.client.force_login(self.actor)
        self.actor.groups.clear()
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_resumen_borrador_cliente_allowlist_y_sin_escrituras(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        created = self.create()
        with CaptureQueriesContext(connection) as queries:
            result = self.summary(created)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(result["cliente"]["total"], "116.00")
        self.assertEqual(result["warnings"][0]["code"], "COSTO_DESCONOCIDO")
        self.assertEqual(result["revision"], 1)
        wire = json.dumps(result["cliente"])
        for key in ("provider", "source_currency", "source_unit_price", "margin_percent", "utilidad", "exchange_rate"):
            self.assertNotIn(key, wire)
        self.assertFalse(self.files())

    def test_resumen_emitida_sin_motor_ni_renderer_y_revision_explicita(self):
        created = self.create()
        self.confirm_and_issue(created)
        with mock.patch("apps.quotes.snapshots._commercial_calculate", side_effect=AssertionError("Sin motor")), mock.patch("apps.quotes.pdf.generate_quote_pdf", side_effect=AssertionError("Sin renderer")):
            summary = self.summary(created)
        self.assertTrue(summary["cliente"]["archived_only"])
        self.assertEqual(summary["cliente"]["total"], "116.00")
        self.send("api_nueva_revision", {"schema_version": 2, "request_id": str(uuid4())}, args=(created["folio"], 1), status=201)
        self.assertEqual(self.summary(created)["revision"], 1)
        self.assertEqual(len(self.summary(created)["revisiones"]), 2)

    def test_resumen_ajeno_y_revocacion_no_exponen_documento(self):
        created = self.create()
        self.client.force_login(self.other)
        self.send("api_resumen_documental", method="get", args=(created["folio"], 1), status=403)
        self.client.force_login(self.actor)
        self.actor.groups.clear()
        self.send("api_resumen_documental", method="get", args=(created["folio"], 1), status=403)

    def test_pdf_borrador_exacto_rechaza_version_estado_y_huella_obsoletos(self):
        created = self.create()
        url = reverse("descargar_pdf_cotizacion", args=[created["folio"]])
        params = self.document_params(created)
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertFalse(self.files())
        for changes in ({"edit_version": 2}, {"state_fingerprint": "b" * 64}, {"expected_status": "ISSUED"}):
            self.assertEqual(self.client.get(url, {**params, **changes}).status_code, 409)
        for key in ("revision", "edit_version", "state_fingerprint"):
            self.assertEqual(self.client.get(url, {k: v for k, v in params.items() if k != key}).status_code, 400)

    def test_pdf_estado_cambia_entre_resumen_y_descarga_no_cambia_documento_silenciosamente(self):
        created = self.create()
        self.confirm_and_issue(created)
        url = reverse("descargar_pdf_cotizacion", args=[created["folio"]])
        self.assertEqual(self.client.get(url, self.document_params(created)).status_code, 409)
        issued = {**created, "status": "ISSUED"}
        response = self.client.get(url, self.document_params(issued))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Quote-Document"], "original")

    def test_correo_exacto_envia_original_no_cambia_destinatario_guardado(self):
        from django.core import mail
        from apps.quotes.snapshots import leer_pdf_emitido
        created = self.create()
        self.confirm_and_issue(created)
        r = QuoteRevision.objects.get(quote__folio=created["folio"], number=1)
        original = leer_pdf_emitido(r)
        body = {**self.document_params({**created, "status": "ISSUED"}), "exact": True,
            "email": "otro@qa.example.invalid", "subject": "QA asunto", "message": "QA mensaje", "attach_pdf": True}
        with mock.patch("apps.quotes.pdf.generate_quote_pdf", side_effect=AssertionError("Sin regenerar")):
            result = self.client.post(reverse("enviar_cotizacion_correo", args=[created["folio"]]), json.dumps(body), content_type="application/json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(mail.outbox[0].attachments[0][1], original)
        self.assertEqual(mail.outbox[0].to, [body["email"]])
        self.assertNotEqual(r.header["client"]["email"], body["email"])

    def test_correo_sin_revision_sin_adjunto_o_estado_obsoleto_no_envia(self):
        from django.core import mail
        created = self.create()
        self.confirm_and_issue(created)
        body = {**self.document_params({**created, "status": "ISSUED"}), "exact": True, "email": "qa@example.invalid", "attach_pdf": True}
        url = reverse("enviar_cotizacion_correo", args=[created["folio"]])
        for changes, status in (({"revision": None}, 400), ({"attach_pdf": False}, 400), ({"expected_status": "DRAFT"}, 409), ({"state_fingerprint": "c" * 64}, 409)):
            self.assertEqual(self.client.post(url, json.dumps({**body, **changes}), content_type="application/json").status_code, status)
        self.assertEqual(len(getattr(mail, "outbox", [])), 0)

    def test_correo_exacto_sin_attach_pdf_adjunta_original(self):
        from django.core import mail
        from apps.quotes.snapshots import leer_pdf_emitido
        created = self.create()
        self.confirm_and_issue(created)
        original = leer_pdf_emitido(QuoteRevision.objects.get(quote__folio=created["folio"], number=1))
        body = {**self.document_params({**created, "status": "ISSUED"}), "exact": True, "email": "qa@example.invalid"}
        result = self.client.post(reverse("enviar_cotizacion_correo", args=[created["folio"]]), json.dumps(body), content_type="application/json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].attachments[0][1], original)

    def test_v1_original_interno_intacto_sin_contexto_cliente_ni_correo(self):
        from apps.quotes.snapshots import leer_pdf_emitido
        original = self.issue(self.draft())
        expected = leer_pdf_emitido(original)
        data = {"folio": self.quote.folio, "revision": 1}
        summary = self.summary(data)
        self.assertEqual(summary["schema_version"], 1)
        self.assertIsNone(summary["cliente"])
        self.assertFalse(summary["emission_enabled"])
        url = reverse("descargar_pdf_cotizacion", args=[self.quote.folio])
        self.assertEqual(self.client.get(url, {"exact": 1, "revision": 1, "edit_version": 1, "expected_status": "ISSUED"}).content, expected)
        sent = self.client.post(reverse("enviar_cotizacion_correo", args=[self.quote.folio]), json.dumps({"email": "qa@example.invalid", "revision": 1}), content_type="application/json")
        self.assertEqual(sent.status_code, 409)
        self.assertIn("histórico interno", sent.json()["error"])  # Motivo explícito, no conflicto genérico.

    def test_preview_transicion_no_crea_revision_ni_reinterpreta_original(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        original = self.issue(self.draft())
        body = {"schema_version": 2, "source_edit_version": 1, "items": [partida_api_manual()],
                "tax_rate": "0.16", "discount_percent": "0", "fx_context_id": None}
        with CaptureQueriesContext(connection) as queries:
            preview = self.send("api_preview_transicion", body, args=(self.quote.folio, 1))
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertFalse(preview["persistida"])
        self.assertEqual(preview["cliente"]["total"], "116.00")
        body["source_edit_version"] = 2
        self.send("api_preview_transicion", body, args=(self.quote.folio, 1), status=409)
        body["source_edit_version"] = 1
        created = self.send("api_nueva_revision", {**body, "request_id": str(uuid4())}, args=(self.quote.folio, 1), status=201)
        self.assertEqual(created["revision"], 2)
        original.refresh_from_db()
        self.assertEqual(original.schema_version, 1)

    def test_plantilla_resumen_no_demo_ni_totales_proveedor_o_cuentas_ficticias(self):
        response = self.client.get(reverse("resumen"))
        for text in ("commercialSummaryApp", "Mis cotizaciones", "BORRADOR", "revisionSelector"):
            self.assertContains(response, text)
        for text in ("COT-2026-0048", "BBVA", "Constructora e Inmobiliaria", "Marca / Dist.", "apiClient.quotes.getAll"):
            self.assertNotContains(response, text)

    def test_identidad_documental_ambigua_y_campos_extra_de_correo_rechazados(self):
        created = self.create()
        url = reverse("descargar_pdf_cotizacion", args=[created["folio"]])
        self.assertEqual(self.client.get(url + "?exact=1&revision=1&revision=2&edit_version=1&expected_status=DRAFT").status_code, 400)
        email_url = reverse("enviar_cotizacion_correo", args=[created["folio"]])
        for body in ('{"email":"qa@example.invalid","revision":1,"revision":2}', '{"email":"qa@example.invalid","revision":1,"cost":"1"}'):
            self.assertEqual(self.client.post(email_url, body, content_type="application/json").status_code, 400)

    def test_fallo_correo_no_reintenta_ni_promete_entrega(self):
        created = self.create()
        self.confirm_and_issue(created)
        url = reverse("enviar_cotizacion_correo", args=[created["folio"]])
        for outcome in (RuntimeError("QA corte"), 0):
            with self.subTest(outcome=type(outcome).__name__), mock.patch("apps.quotes.views.EmailMultiAlternatives.send", **({"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome})) as send, self.assertLogs("apps.quotes.views", level="ERROR"):
                response = self.client.post(url, json.dumps({"email": "qa@example.invalid", "revision": 1}), content_type="application/json")
                self.assertEqual(response.status_code, 500)
                self.assertEqual(send.call_count, 1)


@override_settings(CCONOR_EMPRESA=EMPRESA_QA)
class CommercialAPIConcurrencyTests(CommercialFixtures, TransactionTestCase):
    def test_alta_simultanea_y_fx_no_duplican_recibos(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections
        from django.contrib.auth import get_user_model
        data = alta_comercial()
        barrier = threading.Barrier(2)
        def create():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.actor.pk)
                barrier.wait(timeout=10)
                return api._commercial_create(actor, data)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create) for _ in range(2)]
            results = [f.result(timeout=30) for f in futures]
        self.assertEqual(results[0]["folio"], results[1]["folio"])
        self.assertCountEqual([r["replayed"] for r in results], [True, False])
        self.assertEqual(QuoteAPIRequest.objects.count(), 1)

    def test_intenciones_distintas_misma_clave_solo_una_se_confirma(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections
        from apps.quotes.snapshots import RevisionConflict
        barrier = threading.Barrier(2)
        body = alta_comercial()
        def create(name):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    return api._commercial_create(self.actor, {**body, "client_name": name})["revision"]
                except RevisionConflict:
                    return "CONFLICT"
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create, n) for n in ("Cliente A QA", "Cliente B QA")]
            self.assertCountEqual([f.result(timeout=30) for f in futures], [1, "CONFLICT"])
        self.assertEqual(QuoteAPIRequest.objects.count(), 1)


class CommercialAPIMigrationTests(TransactionTestCase):
    def test_migracion_aditiva_desde_p3_sin_convertir_revisiones(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old = [("quotes", "0003_commercial_revision_v2")]
        try:
            executor.migrate(old)
            historical = executor.loader.project_state(old).apps
            actor = get_user_model().objects.create_user(username="p4_migration_qa")
            quote = historical.get_model("quotes", "Quote").objects.create(advisor_id=actor.pk, folio="P4-LEGACY", client_name="QA")
            revision = historical.get_model("quotes", "QuoteRevision").objects.create(quote_id=quote.pk, number=1,
                request_id=uuid4(), request_fingerprint="a" * 64, header={}, tax_rate=Decimal(".16"),
                issue_date=timezone.localdate(), valid_days=15)
            MigrationExecutor(connection).migrate(latest)
            self.assertEqual(QuoteRevision.objects.get(pk=revision.pk).schema_version, 1)
            self.assertFalse(QuoteSelectionContext.objects.exists())
            self.assertFalse(QuoteAPIRequest.objects.exists())
            context = QuoteSelectionContext.objects.create(actor=actor, kind="FX", payload={
                "value": "17.5", "source": "manual", "date": timezone.now().isoformat()})
            QuoteAPIRequest.objects.create(actor=actor, request_id=uuid4(), operation="FX", fingerprint="b"*64, context=context)
            MigrationExecutor(connection).migrate(old)
            self.assertTrue(historical.get_model("quotes", "QuoteRevision").objects.filter(pk=revision.pk).exists())
        finally:
            MigrationExecutor(connection).migrate(latest)


@override_settings(CCONOR_EMPRESA=EMPRESA_QA)
class ClientesFrecuentesTests(CommercialFixtures, TestCase):
    """18B: «Elegir cliente» lista los clientes de las cotizaciones propias, sin tabla nueva."""
    send = CommercialAPITests.send
    create = CommercialAPITests.create

    def setUp(self):
        super().setUp()
        self.client.force_login(self.actor)

    def clientes(self, status=200, **params):
        response = self.client.get(reverse("api_clientes_frecuentes"), params)
        self.assertEqual(response.status_code, status, response.content.decode()[:500])
        return response.json()

    def alta(self, **cliente):
        datos = {"client_name": "Constructora Norte SA de CV", "client_rfc": "CNO120304AB1", "client_email": "a@cnorte.example.com",
                 "client_contact": "Ing. Ramírez", "client_phone": "618 000 0000", "client_project": "Obra sintética"}
        datos.update(cliente)
        return self.create(alta_comercial(**datos))

    def test_c1_un_cliente_por_rfc_con_los_datos_mas_recientes(self):
        self.alta(client_email="viejo@cnorte.example.com")
        reciente = self.alta(client_name="Constructora Norte", client_email="nuevo@cnorte.example.com", client_contact="Lic. Soto")
        resultado = self.clientes()
        self.assertEqual(resultado["count"], 1)
        cliente = resultado["data"][0]
        self.assertEqual({k: cliente[k] for k in ("business_name", "rfc", "email", "contact", "phone", "source_folio", "quotes_count")},
                         {"business_name": "Constructora Norte", "rfc": "CNO120304AB1", "email": "nuevo@cnorte.example.com",
                          "contact": "Lic. Soto", "phone": "618 000 0000", "source_folio": reciente["folio"], "quotes_count": 2})
        self.assertNotIn("project", cliente)                      # el proyecto cambia en cada cotización
        self.assertNotIn("Obra sintética", json.dumps(resultado))

    def test_c2_sin_rfc_se_agrupa_por_razon_social(self):
        self.alta(client_name="Hotel  Río Nazas", client_rfc="", client_email="")
        self.alta(client_name="hotel río nazas", client_rfc="")
        self.alta(client_name="Hotel Río Nazas", client_rfc="HRN080512PQ3")   # con RFC es otro cliente
        nombres = sorted((c["business_name"], c["rfc"], c["quotes_count"]) for c in self.clientes()["data"])
        self.assertEqual(nombres, [("Hotel Río Nazas", "HRN080512PQ3", 1), ("hotel río nazas", "", 2)])

    def test_c3_solo_cotizaciones_propias(self):
        ajena = self.alta(client_name="Cliente ajeno", client_rfc="CAJ010101AA1")
        quote = Quote.objects.get(folio=ajena["folio"])
        quote.advisor = self.other
        quote.save(update_fields=["advisor"])
        self.alta()
        self.assertEqual([c["business_name"] for c in self.clientes()["data"]], ["Constructora Norte SA de CV"])
        self.client.force_login(self.staff)
        self.assertEqual(self.clientes(), {"success": True, "data": [], "count": 0})

    def test_c4_busqueda_y_orden(self):
        self.alta(client_name="Beta Redes", client_rfc="BRE010101AA1", client_email="compras@beta.example.com", client_contact="Ana")
        for _ in range(2):
            self.alta(client_name="Alfa Seguridad", client_rfc="ASE010101AA1", client_email="", client_contact="Luis")
        self.alta(client_name="Gamma Obras", client_rfc="GOB010101AA1", client_contact="")
        nombres = lambda **p: [c["business_name"] for c in self.clientes(**p)["data"]]
        self.assertEqual(nombres(), ["Gamma Obras", "Alfa Seguridad", "Beta Redes"])          # recientes
        self.assertEqual(nombres(order="count"), ["Alfa Seguridad", "Gamma Obras", "Beta Redes"])
        self.assertEqual(nombres(order="az"), ["Alfa Seguridad", "Beta Redes", "Gamma Obras"])
        self.assertEqual(nombres(q="ase010101"), ["Alfa Seguridad"])                        # RFC
        self.assertEqual(nombres(q="COMPRAS@BETA"), ["Beta Redes"])                         # correo
        self.assertEqual(nombres(q="luis"), ["Alfa Seguridad"])                             # contacto
        self.assertEqual(self.clientes(q="nadie"), {"success": True, "data": [], "count": 0})

    def test_c5_usa_la_ultima_revision_de_cada_cotizacion(self):
        original = self.issue(self.commercial_draft())
        from apps.quotes.snapshots import crear_revision_comercial
        from apps.quotes.tests.test_commercial_persistence import commercial_line
        draft = crear_revision_comercial(original.pk, [commercial_line()], actor=self.actor, request_id=uuid4(),
            tax_rate=Decimal(".16"), discount_percent=Decimal("0"), tipo_cambio=None, exchange_context_id=None)
        self.commercial_edit(draft, client={**draft.header["client"], "business_name": "Cliente revisado", "email": "rev@example.com"})
        resultado = self.clientes()["data"]
        self.assertEqual([(c["business_name"], c["email"], c["quotes_count"]) for c in resultado], [("Cliente revisado", "rev@example.com", 1)])

    def test_c6_entradas_invalidas_sesion_y_metodo(self):
        url = reverse("api_clientes_frecuentes")
        for query in ("order=precio", "q=a&q=b", "otro=1", "q=" + "x" * 201):
            self.assertEqual(self.client.get(url + "?" + query).status_code, 400, query)
        self.assertEqual(self.client.post(url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 401)


@override_settings(CCONOR_EMPRESA=EMPRESA_QA)
class FechasDePrecioTests(CommercialFixtures, TestCase):
    """23A/23C: el borrador dice cuándo se capturó cada precio de proveedor; nada cambia solo."""
    send = CommercialAPITests.send

    def setUp(self):
        super().setUp()
        self.client.force_login(self.actor)

    def test_p1_detalle_trae_la_fecha_de_captura_de_cada_partida(self):
        from apps.quotes.tests.test_commercial_persistence import commercial_line
        draft = self.commercial_draft([commercial_line(kind="API"), commercial_line()])
        snapshots = list(draft.snapshots.order_by("position"))
        detail = self.send("api_guardar_revision", method="get", args=(self.quote.folio, draft.number))
        self.assertEqual([i["captured_at"] for i in detail["items"]], [s.captured_at.isoformat() for s in snapshots])
        self.assertEqual((detail["items"][0]["provider_code"], detail["items"][0]["external_id"]), ("qa", "QA-1"))

    def test_p2_resumen_da_la_captura_mas_antigua_de_proveedor_solo_en_borradores(self):
        from datetime import datetime, timezone as dt_timezone
        from apps.quotes.models import QuoteSnapshot
        from apps.quotes.tests.test_commercial_persistence import commercial_line
        manual, viejo, nuevo = commercial_line(key=uuid4()), commercial_line(kind="API", key=uuid4()), commercial_line(kind="API", key=uuid4(), external_id="QA-2")
        hace_anios = datetime(2020, 1, 1, 12, tzinfo=dt_timezone.utc)
        hace_dias = datetime(2026, 10, 17, 16, 30, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=hace_anios):
            draft = self.commercial_draft([manual])                         # la manual es la más vieja, pero no cuenta
        with mock.patch("django.utils.timezone.now", return_value=hace_dias):
            draft = self.commercial_edit(draft, [manual, viejo])
        draft = self.commercial_edit(draft, [manual, viejo, nuevo])           # la nueva se captura ahora
        fechas = dict(QuoteSnapshot.objects.filter(revision=draft).values_list("line_key", "captured_at"))
        self.assertEqual((fechas[manual.line_key], fechas[viejo.line_key]), (hace_anios, hace_dias))   # se conserva por partida
        resumen = self.send("api_resumen_documental", method="get", args=(self.quote.folio, draft.number))
        self.assertEqual(resumen["prices_captured_at"], hace_dias.isoformat())
        emitida = self.issue(draft)
        resumen = self.send("api_resumen_documental", method="get", args=(self.quote.folio, emitida.number))
        self.assertIsNone(resumen["prices_captured_at"])                    # una emitida no se revisa

    def test_p3_sin_partidas_de_proveedor_no_hay_fecha(self):
        from apps.quotes.tests.test_commercial_persistence import commercial_line
        draft = self.commercial_draft([commercial_line()])
        resumen = self.send("api_resumen_documental", method="get", args=(self.quote.folio, draft.number))
        self.assertIsNone(resumen["prices_captured_at"])
