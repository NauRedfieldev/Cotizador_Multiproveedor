"""QA Prompt 4: integración M7a (API de cotizaciones) + M7b (comparador) + M8 (motor comercial).

Flujo completo con la API real de SYSCOM simulada (muestras con la forma real, sin red):
búsqueda → captura → alta de cotización → preview con cálculos → guardado de borrador →
confirmaciones → emisión → descarga de PDF → envío por correo. También empresas (CCONOR/Torva)
y degradación ante fallo de autenticación de SYSCOM.

Reutiliza las fixtures del equipo: CommercialFixtures (grupos comerciales + actor),
soporte de providers (ApiFalsa, sin_red, credenciales_de_prueba), muestras SYSCOM del M3 y
los helpers de test_api_cotizaciones. La BD es siempre la de pruebas (test_cotizadorrr):
exigir_bd_de_pruebas() falla la prueba si la conexión apunta a otra.
"""
import json
from decimal import Decimal
from unittest import mock
from uuid import uuid4

import httpx
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings, tag
from django.urls import reverse

from apps.providers.models import Provider, ProviderToken
from apps.providers.tests.soporte import (
    ApiFalsa,
    credenciales_de_prueba,
    exigir_bd_de_pruebas,
    respuesta_json,
    sin_red,
)
from apps.providers.tests.test_adaptador_syscom import (
    CENTINELA_SECRETO,
    CLIENT_ID_FALSO,
    HOST,
    RUTA_PRODUCTOS,
    RUTA_TOKEN,
    TOKEN_MUESTRA,
    respuesta_muestra,
)
from apps.quotes.models import Quote, QuoteRevision
from apps.quotes.snapshots import leer_pdf_emitido
from apps.quotes.tests.test_api_cotizaciones import EMPRESA_QA, alta_comercial, cuerpo_alta, partida_api_manual
from apps.quotes.tests.test_commercial_persistence import CommercialFixtures

BASE_URL_QA = "https://developers.syscom.mx/api/v1"
RUTA_TIPOCAMBIO = "/api/v1/tipocambio"
TIPO_CAMBIO_QA = "17.50"
CAMARA_ID = "900001"          # 85.50 USD en la muestra de SYSCOM
CAMARA_COSTO_MXN = Decimal("85.50") * Decimal(TIPO_CAMBIO_QA)   # 1496.25
IVA_QA = Decimal("0.16")
TORVA_QA = {"name": "Torva Telecom QA", "rfc": "", "tagline": "", "phone": "", "email": "",
            "folio_prefix": "TQA", "logo": ""}


def partida_api(context_ref, offer_ref, cantidad=1):
    """Partida v2 capturada desde la API de búsqueda (origen SYSCOM, margen 35%)."""
    return {"line_key": str(uuid4()), "source": {"type": "API", "context_ref": context_ref,
            "offer_ref": offer_ref}, "quantity": cantidad,
            "units": {"source_label": "Pieza", "sale_label": "Pieza", "conversion_factor": "1", "confirmed": True},
            "pricing": {"mode": "MARGIN", "margin_percent": "35"}}


@tag("django_db")
@override_settings(CCONOR_EMPRESA=EMPRESA_QA, COTIZACIONES_EMISION_HABILITADA=True,
                   CCONOR_SMTP_FROM="ventas@qa.example.invalid", TORVA_SMTP_FROM="torva@qa.example.invalid",
                   MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"},
                            "torva": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}})
class Q4Base(CommercialFixtures, TestCase):
    """SYSCOM contra ApiFalsa por el transporte real del orquestador, sin red ni correo real."""

    def setUp(self):
        super().setUp()
        exigir_bd_de_pruebas()
        cache.clear()  # el límite de búsquedas vive en la LocMemCache, que dura toda la suite
        self.enterContext(sin_red())
        self.enterContext(credenciales_de_prueba("syscom", CLIENT_ID=CLIENT_ID_FALSO,
                                                 CLIENT_SECRET=CENTINELA_SECRETO))
        self.api = ApiFalsa()
        self.api.responder(HOST, RUTA_TOKEN, respuesta_muestra("syscom_token"))
        self.api.responder(HOST, RUTA_PRODUCTOS, respuesta_muestra("syscom_productos"))
        # 14A: el comparador puede sugerir el tipo de cambio público de SYSCOM.
        self.api.responder(HOST, RUTA_TIPOCAMBIO, respuesta_json({"normal": "18.54"}))
        self.syscom = Provider.objects.create(code="syscom", name="SYSCOM", base_url=BASE_URL_QA)
        # El M7a/M7b crean httpx.AsyncClient(transport=None): se sustituye ese None por el
        # transporte de ApiFalsa para recorrer el adaptador real de SYSCOM de punta a punta.
        # Cualquier intento de salir a la red real lo corta sin_red() y hace fallar la prueba.
        real = httpx.AsyncClient

        def cliente(**kwargs):
            if kwargs.get("transport") is None:
                kwargs["transport"] = self.api.transport
            return real(**kwargs)

        self.enterContext(mock.patch("apps.providers.service.httpx.AsyncClient", new=cliente))
        self.client.force_login(self.actor)

    # --- helpers -------------------------------------------------------

    def buscar(self, **params):
        response = self.client.get(reverse("api_buscar_productos"),
                                   {"q": "camara ip", "schema_version": "2", "tipo_cambio": TIPO_CAMBIO_QA, **params})
        self.assertEqual(response.status_code, 200, response.content.decode()[:800])
        return response.json()

    def oferta_camara(self, resultado):
        [item] = [o for o in resultado["data"] if o["external_id"] == CAMARA_ID]
        return item

    def detail(self, created):
        response = self.client.get(reverse("api_guardar_revision", args=[created["folio"], created["revision"]]))
        self.assertEqual(response.status_code, 200, response.content.decode()[:800])
        return response.json()["data"]

    def confirm_and_issue(self, created):
        detail = self.detail(created)
        warnings = [w for w in detail["preview"]["warnings"] if w["requires_confirmation"]]
        response = self.client.post(reverse("api_confirmar_comercial", args=[created["folio"], created["revision"]]),
                                    json.dumps({"edit_version": created["edit_version"],
                                                "state_fingerprint": created["state_fingerprint"],
                                                "warnings": [{"code": w["code"], "line_key": w["line_key"]}
                                                              for w in warnings]}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content.decode()[:800])
        response = self.client.post(reverse("api_emitir_revision", args=[created["folio"], created["revision"]]),
                                    json.dumps({"edit_version": created["edit_version"],
                                                "state_fingerprint": created["state_fingerprint"]}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content.decode()[:800])
        return response.json()["data"]


@tag("django_db")
class FlujoCompletoQ4Tests(Q4Base):
    """QA4-01: SYSCOM → búsqueda → alta → preview → borrador → emisión → PDF → correo."""

    def test_q4_01_flujo_completo_punta_a_punta(self):
        resultado = self.buscar()
        item = self.oferta_camara(resultado)
        # M3 + M2 + M6: la oferta llega convertida a MXN con el tipo de cambio capturado.
        self.assertEqual((item["provider"], item["currency"], item["price_original"], item["vat"]["vat_basis"]),
                         ("syscom", "USD", "85.50", "NET"))
        self.assertEqual(Decimal(item["precio_mxn"]), CAMARA_COSTO_MXN)

        body_alta = cuerpo_alta(items=[partida_api(resultado["context_ref"], item["offer_ref"])])
        body_alta.update(schema_version=2, tax_rate="0.16", discount_percent="0",
                         fx_context_id=resultado.get("fx_context_id"), issue_date="2026-10-23")
        response = self.client.post(reverse("api_crear_cotizacion"), json.dumps(body_alta),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content.decode()[:800])
        created = response.json()["data"]
        self.assertTrue(created["folio"].startswith("COT-"))
        self.assertEqual((created["revision"], created["status"], created["edit_version"]), (1, "DRAFT", 1))

        # M8: preview con los cuatro cálculos coherentes (margen 35% sobre el costo MXN).
        preview = self.detail(created)["preview"]["resultado"]
        subtotal = Decimal(str(preview["subtotal_venta"]))
        venta_unitaria = (CAMARA_COSTO_MXN / (Decimal("1") - Decimal("0.35"))).quantize(Decimal("0.01"))
        self.assertEqual(subtotal, venta_unitaria)
        self.assertEqual(Decimal(str(preview["iva"])), (subtotal * IVA_QA).quantize(Decimal("0.01")))
        self.assertEqual(Decimal(str(preview["total"])), subtotal + Decimal(str(preview["iva"])))
        self.assertEqual(Decimal(str(preview["utilidad_bruta"])), subtotal - CAMARA_COSTO_MXN)

        # Guardado de borrador: la cantidad pasa a 2 (reemplazo completo, source EXISTING).
        edit = {k: v for k, v in body_alta.items() if k not in ("schema_version", "request_id", "notes")}
        edit["items"][0]["source"] = {"type": "EXISTING"}
        edit["items"][0]["quantity"] = 2
        edit["edit_version"] = 1
        response = self.client.put(reverse("api_guardar_revision", args=[created["folio"], 1]),
                                   json.dumps(edit), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content.decode()[:800])
        guardado = response.json()["data"]
        self.assertEqual(guardado["edit_version"], 2)
        # El importe de línea se redondea por partida sobre la venta sin redondear:
        # 1496.25/0.65 × 2 = 4603.8461… → 4603.85 (distinto de 4603.84 = 2301.92 × 2).
        subtotal2 = Decimal(str(self.detail(created)["preview"]["resultado"]["subtotal_venta"]))
        venta_sin_redondear = CAMARA_COSTO_MXN / (Decimal("1") - Decimal("0.35"))
        self.assertEqual(subtotal2, (venta_sin_redondear * 2).quantize(Decimal("0.01")))

        # PDF del borrador: existe y es un PDF, sin archivo archivado todavía.
        pdf_borrador = self.client.get(reverse("descargar_pdf_cotizacion", args=[created["folio"]]))
        self.assertEqual(pdf_borrador.status_code, 200)
        self.assertTrue(pdf_borrador.content.startswith(b"%PDF-"))
        self.assertEqual(pdf_borrador["X-Quote-Document"], "draft-or-historical")

        # Emisión y PDF oficial (bytes originales archivados).
        emitted = self.confirm_and_issue({**created, **guardado})
        self.assertEqual(emitted["status"], "ISSUED")
        pdf_oficial = self.client.get(reverse("descargar_pdf_cotizacion", args=[created["folio"]]))
        self.assertEqual(pdf_oficial.status_code, 200)
        self.assertEqual(pdf_oficial["X-Quote-Document"], "original")

        # Correo con el original archivado, remitente y asunto de la empresa.
        response = self.client.post(reverse("enviar_cotizacion_correo", args=[created["folio"]]),
                                    json.dumps({"email": "cliente@qa.example.invalid", "revision": 1}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content.decode()[:800])
        self.assertEqual(len(mail.outbox), 1)
        enviado = mail.outbox[0]
        self.assertEqual(enviado.to, ["cliente@qa.example.invalid"])
        self.assertEqual(enviado.from_email, "ventas@qa.example.invalid")
        self.assertIn(created["folio"], enviado.subject)
        original = leer_pdf_emitido(QuoteRevision.objects.get(quote__folio=created["folio"], number=1))
        self.assertEqual(enviado.attachments[0][1], original)

        # Integración SYSCOM: el token se pidió una sola vez y quedó cacheado en BD.
        self.assertEqual(self.api.rutas_pedidas().count(f"{HOST}{RUTA_TOKEN}"), 1)
        token = ProviderToken.objects.get(provider=self.syscom)
        self.assertEqual(token.access_token, TOKEN_MUESTRA)


@tag("django_db")
class ComparadorQ4Tests(Q4Base):
    """QA4-02/03: el comparador server-rendered (M7b) consulta SYSCOM real simulado."""

    def test_q4_02_comparador_pinta_ofertas_de_syscom_convertidas(self):
        response = self.client.get(reverse("comparador"), {"q": "camara ip", "tipo_cambio": TIPO_CAMBIO_QA})
        self.assertEqual(response.status_code, 200, response.content.decode()[:800])
        self.assertTemplateUsed(response, "comparador.html")
        camara = [o for o in response.context["ofertas"] if o.oferta.external_id == CAMARA_ID][0]
        self.assertEqual(camara.oferta.provider_code, "syscom")
        self.assertEqual(camara.proveedor_nombre, "SYSCOM")
        self.assertEqual(camara.precio_comparable, CAMARA_COSTO_MXN)
        self.assertContains(response, "SYSCOM")

    def test_q4_03_syscom_sin_auth_se_degrada_con_aviso_nunca_500(self):
        """Un 401 de SYSCOM en el token no rompe el comparador: queda como proveedor caído."""
        self.api.responder(HOST, RUTA_TOKEN, respuesta_json({}, status=401))
        response = self.client.get(reverse("comparador"), {"q": "camara ip", "tipo_cambio": TIPO_CAMBIO_QA})
        self.assertEqual(response.status_code, 503)          # único proveedor caído
        self.assertEqual(response.context["ofertas"], [])
        self.assertEqual(response.context["proveedores_caidos"], ["syscom"])
        self.assertEqual(response.context["proveedores_caidos_nombres"], ["SYSCOM"])
        self.assertContains(response, response.context["error"], status_code=503)


@tag("django_db")
class EmpresasQ4Tests(Q4Base):
    """QA4-04/05: configuración de empresas en el alta y en el correo."""

    def test_q4_04_cconor_sin_nombre_no_crea_cotizacion(self):
        antes = Quote.objects.count()
        with override_settings(CCONOR_EMPRESA={**EMPRESA_QA, "name": "   "}):
            response = self.client.post(reverse("api_crear_cotizacion"), json.dumps(alta_comercial()),
                                        content_type="application/json")
        self.assertEqual(response.status_code, 503, response.content.decode()[:800])
        self.assertEqual(response.json()["code"], "UNAVAILABLE")
        self.assertEqual(Quote.objects.count(), antes)

    def test_q4_05_torva_consuje_folio_y_correo_propios(self):
        with override_settings(TORVA_EMPRESA=TORVA_QA):
            response = self.client.post(reverse("api_crear_cotizacion"),
                                        json.dumps(alta_comercial(company_key="torva")),
                                        content_type="application/json")
            self.assertEqual(response.status_code, 201, response.content.decode()[:800])
            created = response.json()["data"]
            self.assertRegex(created["folio"], r"^TQA-\d{4}-\d{4}$")
            self.confirm_and_issue(created)
            response = self.client.post(reverse("enviar_cotizacion_correo", args=[created["folio"]]),
                                        json.dumps({"email": "cliente@qa.example.invalid", "revision": 1}),
                                        content_type="application/json")
            self.assertEqual(response.status_code, 200, response.content.decode()[:800])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].from_email, "torva@qa.example.invalid")
        self.assertIn(TORVA_QA["name"], mail.outbox[0].subject)
        stored = QuoteRevision.objects.get(quote__folio=created["folio"]).header["company"]
        self.assertEqual((stored["key"], stored["name"]), ("torva", TORVA_QA["name"]))
