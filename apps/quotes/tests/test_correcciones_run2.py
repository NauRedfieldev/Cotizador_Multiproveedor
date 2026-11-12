"""Correcciones de la auditoría run-2 en cotizaciones (docs/37).

#19: todo PDF (v1, v2 o histórico) exige view_commercial_costs, como la API.
#17: una cotización ajena responde igual que un folio inexistente (404); sin rol, 403 en ambos.
#16, #20: límites de correo y de PDF por usuario (429). #21: demo PDF solo staff, con DEBUG y folio corto.
"""
import json
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache, caches
from django.test import Client, TestCase, override_settings

from apps.providers.models import Provider
from apps.providers.service import MAX_BYTES_RESPUESTA
from apps.quotes.tests.test_commercial_persistence import CommercialFixtures, commercial_line
from apps.quotes.tests.test_snapshots import partida
from apps.quotes.views import LIMITE_CORREOS_POR_HORA, LIMITE_PDF_POR_MINUTO, tipo_cambio_sugerido

FOLIO_INEXISTENTE = "NOEXISTE-2026-9999"


class PdfExigeCostosTests(CommercialFixtures, TestCase):
    """Auditoría run-2 #19: el PDF v1 lleva costo de origen, proveedor y stock."""

    def setUp(self):
        super().setUp()
        self.v1 = self.draft([partida(precio="1234.5678", cantidad="3")])
        self.url = f"/cotizaciones/{self.quote.folio}/pdf/?revision={self.v1.number}"

    def test_dueno_con_rol_descarga(self):
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_dueno_con_rol_revocado_no_descarga(self):
        self.actor.groups.clear()
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_sin_rol_comercial_no_descarga(self):
        tecnico = get_user_model().objects.create_user("staff_tecnico_qa", is_staff=True)
        self.client.force_login(tecnico)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_con_rol_descarga_cotizacion_ajena(self):
        self.client.force_login(self.staff)  # Personal CCONOR + is_staff (salto de dueño por diseño)
        self.assertEqual(self.client.get(self.url).status_code, 200)


class FolioAjenoComoInexistenteTests(CommercialFixtures, TestCase):
    """Auditoría run-2 #17: 403 frente a 404 revelaba qué folios existen (son correlativos)."""

    def setUp(self):
        super().setUp()
        self.issue(self.commercial_draft([commercial_line(kind="API")]))
        U = get_user_model().objects
        self.principales = {
            "B_mismo_rol": self.other,
            "sin_rol": U.create_user("sin_rol_qa"),
            "staff_sin_rol": U.create_user("staff_sin_rol_qa", is_staff=True),
        }

    @staticmethod
    def rutas(folio):
        b = f"/api/v1/quotes/{folio}"
        return [
            ("get", f"{b}/", None), ("get", f"{b}/revisiones/1/", None), ("put", f"{b}/revisiones/1/", {}),
            ("get", f"{b}/revisiones/1/resumen/", None), ("post", f"{b}/revisiones/1/preview/", {}),
            ("post", f"{b}/revisiones/1/confirmaciones/", {}),
            ("post", f"{b}/revisiones/1/emitir/", {"edit_version": 1}),
            ("post", f"{b}/revisiones/1/nueva/", {"request_id": "00000000-0000-4000-8000-000000000000"}),
            ("post", f"{b}/revisiones/1/nueva/preview/", {}),
            ("get", f"/cotizaciones/{folio}/pdf/?revision=1", None),
            ("post", f"/api/cotizaciones/{folio}/enviar-correo/", {"email": "b@example.invalid", "revision": 1}),
        ]

    def llamar(self, metodo, url, cuerpo):
        extra = {} if cuerpo is None else {"data": json.dumps(cuerpo), "content_type": "application/json"}
        return getattr(self.client, metodo)(url, **extra).status_code

    def test_ajeno_e_inexistente_responden_igual(self):
        for nombre, usuario in self.principales.items():
            self.client.force_login(usuario)
            for (metodo, url, cuerpo), (_, falsa, _) in zip(self.rutas(self.quote.folio), self.rutas(FOLIO_INEXISTENTE)):
                with self.subTest(principal=nombre, metodo=metodo, ruta=falsa):
                    real, inexistente = self.llamar(metodo, url, cuerpo), self.llamar(metodo, falsa, cuerpo)
                    self.assertEqual(real, inexistente)
                    self.assertNotIn(real, (200, 201))

    def test_con_rol_el_ajeno_es_404_y_sin_rol_403(self):
        detalle = f"/api/v1/quotes/{self.quote.folio}/"
        self.client.force_login(self.other)
        respuesta = self.client.get(detalle)
        self.assertEqual(respuesta.status_code, 404)
        self.assertEqual(respuesta.json()["code"], "NOT_FOUND")
        self.client.force_login(self.principales["sin_rol"])
        self.assertEqual(self.client.get(detalle).status_code, 403)

    def test_el_dueno_sigue_entrando(self):
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(f"/api/v1/quotes/{self.quote.folio}/").status_code, 200)
        self.assertEqual(self.client.get(f"/cotizaciones/{self.quote.folio}/pdf/?revision=1").status_code, 200)


class CorreoConLimiteTests(CommercialFixtures, TestCase):
    """Auditoría run-2 #16 (sin límite de envíos), asunto con salto de línea (500) y punto 9 (POST en DEBUG)."""

    def setUp(self):
        super().setUp()
        caches["limites"].clear()
        self.addCleanup(caches["limites"].clear)
        self.issue(self.commercial_draft())
        self.client.force_login(self.actor)
        self.url = f"/api/cotizaciones/{self.quote.folio}/enviar-correo/"

    def enviar(self, **cuerpo):
        return self.client.post(self.url, json.dumps({"revision": 1, "email": "cliente@example.invalid", **cuerpo}),
                                content_type="application/json")

    def test_el_correo_21_de_la_hora_da_429(self):
        codigos = [self.enviar(email=f"destino{i}@example.invalid").status_code for i in range(LIMITE_CORREOS_POR_HORA)]
        self.assertEqual(set(codigos), {200})
        respuesta = self.enviar()
        self.assertEqual(respuesta.status_code, 429)
        self.assertFalse(respuesta.json()["success"])
        self.assertEqual(len(mail.outbox), LIMITE_CORREOS_POR_HORA)

    def test_el_limite_es_por_usuario(self):
        caches["limites"].set(f"correo:usuario:{self.actor.pk}", LIMITE_CORREOS_POR_HORA, 3600)
        self.assertEqual(self.enviar().status_code, 429)
        self.client.force_login(self.staff)  # otro usuario (staff con rol) conserva su cupo
        self.assertEqual(self.enviar().status_code, 200)

    def test_asunto_con_salto_de_linea_da_400(self):
        for asunto in ("Hola\r\nBcc: espia@example.invalid", "Hola\nX: 1", "Hola\rX: 1"):
            with self.subTest(asunto=asunto):
                self.assertEqual(self.enviar(subject=asunto).status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_con_debug_el_mensaje_no_aparece_en_la_pagina_de_error(self):
        cliente = Client(raise_request_exception=False)
        cliente.force_login(self.actor)
        with override_settings(DEBUG=True), mock.patch("apps.quotes.views.render_to_string", side_effect=RuntimeError("QA")):
            respuesta = cliente.post(self.url, json.dumps({"revision": 1, "email": "x@example.invalid",
                                                           "message": "MENSAJE-PRIVADO-QA"}),
                                     content_type="application/json")
        self.assertEqual(respuesta.status_code, 500)
        self.assertEqual(respuesta["Content-Type"], "application/json")  # sin página técnica
        self.assertNotIn("MENSAJE-PRIVADO-QA", respuesta.content.decode(errors="replace"))


class PdfConLimiteTests(CommercialFixtures, TestCase):
    """Auditoría run-2 #20: cada borrador se vuelve a generar; sin límite, un usuario ocupaba el único worker."""

    def setUp(self):
        super().setUp()
        caches["limites"].clear()
        self.addCleanup(caches["limites"].clear)
        self.v1 = self.draft()
        self.client.force_login(self.actor)
        self.url = f"/cotizaciones/{self.quote.folio}/pdf/?revision={self.v1.number}"

    def test_la_descarga_11_del_minuto_da_429(self):
        codigos = [self.client.get(self.url).status_code for _ in range(LIMITE_PDF_POR_MINUTO)]
        self.assertEqual(set(codigos), {200})
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 429)
        self.assertIn("error", respuesta.json())


class DemoPdfTests(TestCase):
    """Auditoría run-2 #21: con DEBUG=True el demo era anónimo y un folio largo daba un 500 técnico."""

    def setUp(self):
        U = get_user_model().objects
        self.staff = U.create_user("staff_demo_qa", is_staff=True)
        self.normal = U.create_user("normal_demo_qa")

    def test_anonimo_va_al_login(self):
        with override_settings(DEBUG=True):
            self.assertEqual(self.client.get("/demo/cotizaciones/DEMO/pdf/").status_code, 302)

    def test_solo_staff_con_debug_y_folio_corto(self):
        self.client.force_login(self.staff)
        with override_settings(DEBUG=True):
            self.assertEqual(self.client.get("/demo/cotizaciones/DEMO/pdf/").status_code, 200)
            self.assertEqual(self.client.get("/demo/cotizaciones/" + "Z" * 41 + "/pdf/").status_code, 404)
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.get("/demo/cotizaciones/DEMO/pdf/").status_code, 404)
        self.client.force_login(self.normal)
        with override_settings(DEBUG=True):
            self.assertEqual(self.client.get("/demo/cotizaciones/DEMO/pdf/").status_code, 404)


class TipoCambioConTopeTests(TestCase):
    """Auditoría run-2 #18: el tipo de cambio de SYSCOM usa el mismo tope de respuesta que search_all."""

    async def test_respuesta_enorme_del_tipo_de_cambio_se_corta(self):
        await Provider.objects.acreate(code="syscom", name="SYSCOM", base_url="https://developers.syscom.mx/api/v1")
        await cache.aclear()
        real = httpx.AsyncClient

        def cliente(**kwargs):
            kwargs["transport"] = httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"A" * (MAX_BYTES_RESPUESTA + 1)))
            return real(**kwargs)

        with mock.patch("apps.quotes.views.httpx.AsyncClient", new=cliente), \
                self.assertLogs("apps.quotes.views", level="WARNING") as registros:
            valor, fallo = await tipo_cambio_sugerido()
        self.assertEqual((valor, fallo), (None, True))
        self.assertIn("RespuestaDemasiadoGrande", registros.output[0])


class PdfDeBorradorTests(CommercialFixtures, TestCase):
    """docs/37 §9 (auditoría run-2 #20): caché del PDF ya generado, máximo 2 a la vez y tope de texto total."""

    def setUp(self):
        super().setUp()
        for alias in ("pdfs", "limites"):
            caches[alias].clear()
            self.addCleanup(caches[alias].clear)
        self.v1 = self.draft()
        self.client.force_login(self.actor)
        self.url = f"/cotizaciones/{self.quote.folio}/pdf/?revision={self.v1.number}"

    def test_el_mismo_borrador_se_genera_una_sola_vez(self):
        from apps.quotes import pdf

        with mock.patch("apps.quotes.pdf.generate_quote_pdf", wraps=pdf.generate_quote_pdf) as generar:
            primero, segundo = self.client.get(self.url), self.client.get(self.url)
        self.assertEqual((primero.status_code, segundo.status_code), (200, 200))
        self.assertEqual(generar.call_count, 1)
        self.assertEqual(primero.content, segundo.content)

    def test_si_el_borrador_cambia_se_genera_de_nuevo(self):
        with mock.patch("apps.quotes.pdf.generate_quote_pdf", return_value=b"%PDF-QA") as generar:
            self.client.get(self.url)
            self.edit(self.v1, [partida(precio="2.5")])
            self.client.get(self.url)
        self.assertEqual(generar.call_count, 2)

    def test_con_dos_pdf_en_curso_el_tercero_da_503_y_el_de_cache_sale(self):
        from apps.quotes import snapshots

        self.client.get(self.url)  # queda en caché
        otro = self.draft([partida(precio="7")])
        tomados = [snapshots._GENERANDO.acquire(blocking=False) for _ in range(snapshots.MAX_PDF_SIMULTANEOS)]
        try:
            ocupado = self.client.get(f"/cotizaciones/{self.quote.folio}/pdf/?revision={otro.number}")
            de_cache = self.client.get(self.url)
        finally:
            for _ in tomados:
                snapshots._GENERANDO.release()
        self.assertEqual(tomados, [True] * snapshots.MAX_PDF_SIMULTANEOS)
        self.assertEqual(ocupado.status_code, 503)
        self.assertEqual(ocupado["Retry-After"], "5")
        self.assertIn("error", ocupado.json())
        self.assertEqual(de_cache.status_code, 200)

    def test_tope_de_texto_total_en_borradores_v1_y_v2(self):
        from django.core.exceptions import ValidationError

        from apps.quotes.snapshot_values import MAX_TEXTO_TOTAL

        n = 25
        por_partida = MAX_TEXTO_TOTAL // n
        v1 = lambda extra: [partida(description="d" * (por_partida - len("Equipo de prueba") + (extra if i == 0 else 0)))
                            for i in range(n)]
        v2 = lambda extra: [commercial_line(description="d" * (por_partida - len("Concepto sintético") + (extra if i == 0 else 0)))
                            for i in range(n)]
        self.assertIsNotNone(self.draft(v1(0)))
        self.assertIsNotNone(self.commercial_draft(v2(0)))
        for crear, partes in ((self.draft, v1(1)), (self.commercial_draft, v2(1))):
            with self.subTest(crear=crear.__name__), self.assertRaisesMessage(ValidationError, "150 000 caracteres"):
                crear(partes)
