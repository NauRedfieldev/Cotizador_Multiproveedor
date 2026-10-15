"""M8: datos sintéticos y almacenamiento temporal, sin APIs ni archivos de usuario."""

import hashlib
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, Inexact, Rounded, localcontext
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from apps.providers.contracts import ProviderOffer, ProviderResult
from apps.quotes.comparison import comparar, tipo_cambio_manual
from apps.quotes.models import Quote, QuoteItem, QuoteRevision, QuoteSnapshot
from apps.quotes.pdf import build_demo_quote, generate_quote_pdf, get_quote_context, money, unit_money
from apps.quotes.snapshot_values import decimal_limit
from apps.quotes.snapshots import (
    ArchivedPDFError, PartidaCapturada, RevisionConflict, acrear_borrador, aemitir_revision,
    aobtener_documento, contexto_revision, crear_borrador, crear_revision, emitir_revision,
    guardar_borrador, leer_pdf_emitido, obtener_documento,
)

FECHA = datetime(2026, 10, 12, 12, tzinfo=dt_timezone.utc)
COMPANY = {"name": "Empresa de prueba", "rfc": "", "tagline": "Datos sintéticos",
           "phone": "", "email": ""}


def partida(precio="1.004", cantidad="10", moneda="MXN", **kwargs):
    return PartidaCapturada("Equipo de prueba", Decimal(precio), moneda, Decimal(cantidad), **kwargs)


class SnapshotFixtures:
    def setUp(self):
        super().setUp()
        tmp_root = Path(tempfile.gettempdir()) / "opencode"
        self.tmp = tempfile.TemporaryDirectory(prefix="m8-", dir=tmp_root if tmp_root.is_dir() else None)
        self.addCleanup(self.tmp.cleanup)
        self.config = override_settings(QUOTE_PDF_ROOT=self.tmp.name,
                                       MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}})
        self.config.enable()
        self.addCleanup(self.config.disable)
        self.actor = get_user_model().objects.create_user(username="asesor_prueba")
        self.other = get_user_model().objects.create_user(username="otro_prueba")
        self.staff = get_user_model().objects.create_user(username="admin_prueba", is_staff=True)
        self.quote = Quote.objects.create(advisor=self.actor, client_name="Cliente de prueba")

    def draft(self, parts=None, **kwargs):
        datos = dict(actor=self.actor, company=COMPANY.copy(), conditions=["Condición de prueba."])
        datos.update(kwargs)
        return crear_borrador(self.quote.pk, [partida()] if parts is None else parts, **datos)

    def issue(self, revision=None):
        revision = revision or self.draft()
        return emitir_revision(revision.pk, actor=self.actor, expected_version=revision.edit_version)

    def edit(self, revision, parts=None, **kwargs):
        datos = dict(actor=self.actor, expected_version=revision.edit_version, company=revision.header["company"],
                     conditions=revision.header["conditions"], client=revision.header["client"], tipo_cambio=None,
                     tax_rate=revision.tax_rate, issue_date=revision.issue_date, valid_days=revision.valid_days)
        datos.update(kwargs)
        return guardar_borrador(revision.pk, [partida()] if parts is None else parts, **datos)

    def files(self):
        return list(Path(self.tmp.name).rglob("*.pdf"))


class DecimalLimitsTests(SimpleTestCase):
    def test_seis_ocho_decimales_y_ceros_finales(self):
        self.assertEqual(decimal_limit(Decimal("85.5000000000"), 18, 6), Decimal("85.500000"))
        self.assertEqual(decimal_limit(Decimal("17.12345678"), 18, 8), Decimal("17.12345678"))

    def test_no_redondea_exceso_significativo(self):
        for value, places in [("1.0000001", 6), ("17.123456789", 8), ("1.001", 2)]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                decimal_limit(Decimal(value), 18, places)

    def test_no_float_ni_rangos_excesivos(self):
        for value in [1.1, "1", True, Decimal("-1"), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"),
                      Decimal("1000000000000"), Decimal("1E1000000")]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                decimal_limit(value, 18, 6)

    def test_presentacion_comercial_y_unitario_preciso(self):
        self.assertEqual(money(Decimal("1.005")), "$1.01")
        self.assertEqual(unit_money(Decimal("1.00400000")), "$1.004")
        self.assertEqual(unit_money(Decimal("10")), "$10.00")


class SnapshotFlowTests(SnapshotFixtures, TestCase):
    def test_captura_no_toca_partidas_legacy_y_conserva_originales(self):
        legacy = QuoteItem.objects.create(quote=self.quote, description="Histórico", unit_price=Decimal("2"))
        draft = self.draft([partida(stock=0), partida(precio="2", stock=None)])
        snapshots = list(draft.snapshots.all())
        self.assertEqual([s.stock for s in snapshots], [0, None])
        self.assertEqual(snapshots[0].source_unit_price, Decimal("1.004000"))
        legacy.refresh_from_db()
        self.assertEqual(legacy.unit_price, Decimal("2"))
        self.assertFalse(self.files())

    def test_emision_calcula_por_linea_y_archiva(self):
        issued = self.issue(self.draft([partida(), partida(precio="0.005", cantidad="1")]))
        self.assertEqual((issued.subtotal, issued.tax, issued.total), (Decimal("10.05"), Decimal("1.61"), Decimal("11.66")))
        self.assertEqual([s.amount for s in issued.snapshots.all()], [Decimal("10.04"), Decimal("0.01")])
        data = leer_pdf_emitido(issued)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), issued.pdf_sha256)
        self.assertEqual(len(self.files()), 1)
        self.assertNotIn(self.actor.username, issued.pdf.name)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ISSUED)

    def test_precision_usd_y_tipo_manual_desde_m6(self):
        offer = ProviderOffer("nuevo", "123", "Equipo", Decimal("382.533200"), "USD", stock=0)
        tipo = tipo_cambio_manual("17.12345678", fecha=FECHA)
        comp = comparar([ProviderResult("nuevo", offers=(offer,))], tipo_cambio=tipo)
        line = PartidaCapturada.desde_comparada(comp.por_proveedor["nuevo"][0], Decimal("2"), nombre_proveedor="Proveedor nuevo")
        draft = self.draft([line], tipo_cambio=tipo)
        self.assertTrue(draft.snapshots.get().es_mejor_precio)
        self.assertEqual(draft.exchange_rate, tipo.valor)
        context = contexto_revision(draft)
        with localcontext() as ctx:
            ctx.prec = 64
            expected = (offer.price * tipo.valor * Decimal("2")).quantize(Decimal("0.01"))
        self.assertEqual(context["subtotal"], expected)
        self.assertEqual(self.issue(draft).subtotal, expected)

    def test_usd_sin_tipo_y_limites_no_dejan_filas(self):
        for line in [partida(moneda="USD"), partida(precio="1.0000001"), partida(cantidad="0"),
                     partida(cantidad="0.001"), partida(moneda="EUR"), partida(stock=-1), partida(stock=True)]:
            with self.subTest(line=line), self.assertRaises(ValidationError):
                self.draft([line])
        self.assertEqual(QuoteRevision.objects.count(), 0)
        self.assertEqual(QuoteSnapshot.objects.count(), 0)

    def test_tipo_con_exceso_de_precision_se_rechaza(self):
        with self.assertRaises(ValidationError):
            self.draft([partida(moneda="USD")], tipo_cambio=tipo_cambio_manual("17.123456789", fecha=FECHA))

    def test_maximos_representables_sin_overflow_de_totales(self):
        tipo = tipo_cambio_manual("9999999999.99999999", fecha=FECHA)
        line = partida(precio="999999999999.999999", cantidad="99999999.99", moneda="USD")
        issued = self.issue(self.draft([line] * 500, tipo_cambio=tipo, tax_rate=Decimal("1")))
        self.assertTrue(issued.total.is_finite())
        self.assertLess(issued.total, Decimal("1e36"))
        with self.assertRaises(ValidationError):
            self.draft([line] * 501, tipo_cambio=tipo)

    def test_fecha_limite_no_deja_borrador_inutilizable(self):
        with self.assertRaises(ValidationError):
            self.draft(issue_date=date.max, valid_days=15)
        self.assertFalse(QuoteRevision.objects.exists())

    def test_oferta_no_persiste_raw_y_mxn_no_aplica_fx(self):
        oferta = ProviderOffer("nuevo", "uno", "Prueba", Decimal("1.004"), "MXN", raw={"interno": "no guardar"})
        line = PartidaCapturada.desde_oferta(oferta, Decimal("10"), nombre_proveedor="Nuevo")
        issued = self.issue(self.draft([line], tipo_cambio=tipo_cambio_manual("18", fecha=FECHA)))
        self.assertEqual(issued.subtotal, Decimal("10.04"))
        self.assertNotIn("raw", {f.name for f in QuoteSnapshot._meta.fields})

    def test_cabecera_fecha_vigencia_y_lista_invalidas(self):
        for kwargs in [{"company": {}}, {"conditions": [None]}, {"valid_days": True}, {"valid_days": 0},
                       {"issue_date": "2026-10-12"}, {"tax_rate": Decimal("1.1")}, {"company": {**COMPANY, "raw": {}}}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.draft(**kwargs)
        with self.assertRaises(ValidationError):
            self.draft([])

    def test_borrador_edita_y_detecta_version_obsoleta(self):
        draft = self.draft([partida(), partida(precio="2")])
        edited = self.edit(draft, [partida(cantidad="1")], conditions=["Condición nueva"])
        self.assertEqual(edited.edit_version, 2)
        self.assertEqual(edited.snapshots.count(), 1)
        with self.assertRaises(RevisionConflict):
            self.edit(draft)
        self.assertEqual(edited.snapshots.get().quantity, Decimal("1"))

    def test_edicion_invalida_revierte_estado_anterior(self):
        draft = self.draft()
        with self.assertRaises(ValidationError):
            self.edit(draft, tax_rate=Decimal("2"))
        draft.refresh_from_db()
        self.assertEqual(draft.edit_version, 1)
        self.assertEqual(draft.snapshots.count(), 1)

    def test_cambios_posteriores_no_alteran_emision(self):
        issued = self.issue()
        data = leer_pdf_emitido(issued)
        self.quote.client_name = "Cliente actualizado"
        self.quote.folio = "FOLIO-NUEVO"
        self.quote.tax_rate = Decimal("0.08")
        self.quote.save()
        self.actor.first_name = "Nombre actualizado"
        self.actor.save()
        issued = QuoteRevision.objects.get(pk=issued.pk)
        self.assertEqual(contexto_revision(issued)["client"]["business_name"], "Cliente de prueba")
        self.assertNotEqual(contexto_revision(issued)["folio"], self.quote.folio)
        self.assertEqual(leer_pdf_emitido(issued), data)

    def test_modificar_emitida_crea_nueva_revision(self):
        first = self.issue()
        original = leer_pdf_emitido(first)
        second = crear_revision(first.pk, actor=self.actor)
        self.assertEqual((second.number, second.based_on_id, second.status), (2, first.pk, "DRAFT"))
        self.assertFalse(second.pdf)
        self.assertIsNone(second.subtotal)
        second = self.edit(second, [partida(cantidad="1")])
        self.issue(second)
        self.assertEqual(leer_pdf_emitido(first), original)
        self.assertEqual(len(self.files()), 2)
        with self.assertRaises(RevisionConflict):
            self.edit(first)

    def test_idempotencia_creacion_emision_y_clone(self):
        key = uuid4()
        draft = self.draft(request_id=key)
        self.assertEqual(self.draft(request_id=key).pk, draft.pk)
        with self.assertRaises(RevisionConflict):
            self.draft([partida(precio="2")], request_id=key)
        first = self.issue(draft)
        with patch("apps.quotes.pdf.generate_quote_pdf", side_effect=AssertionError("No regenerar")):
            self.assertEqual(self.issue(first).pk, first.pk)
        key2 = uuid4()
        second = crear_revision(first.pk, actor=self.actor, request_id=key2)
        self.assertEqual(crear_revision(first.pk, actor=self.actor, request_id=key2).pk, second.pk)
        self.assertEqual(QuoteRevision.objects.count(), 2)
        self.assertEqual(len(self.files()), 1)

    def test_reintento_clone_otro_dia_no_depende_de_datos_vivos(self):
        first = self.issue()
        key = uuid4()
        second = crear_revision(first.pk, actor=self.actor, request_id=key)
        self.actor.first_name = "Cambio posterior"
        self.actor.save()
        with patch("apps.quotes.snapshots.timezone.localdate", return_value=second.issue_date + timedelta(days=1)):
            self.assertEqual(crear_revision(first.pk, actor=self.actor, request_id=key).pk, second.pk)
        with self.assertRaises(RevisionConflict):
            crear_revision(first.pk, actor=self.actor, request_id=first.request_id)

    def test_modelos_y_operaciones_masivas_no_sobrescriben_emisiones(self):
        issued = self.issue()
        snap = issued.snapshots.get()
        issued.header["client"]["business_name"] = "Cambiar"
        for action in [issued.save, issued.delete, snap.delete,
                       lambda: QuoteRevision.objects.filter(pk=issued.pk).update(header={}),
                       lambda: QuoteSnapshot.objects.filter(pk=snap.pk).delete(),
                       lambda: QuoteSnapshot.objects.bulk_update([snap], ["name"]),
                       lambda: QuoteSnapshot.objects.bulk_create([])]:
            with self.assertRaises(ValidationError):
                action()
        snap.quantity = Decimal("2")
        with self.assertRaises(ValidationError):
            snap.save()
        draft = self.draft()
        snap.revision = draft
        with self.assertRaises(ValidationError):
            snap.delete()
        with self.assertRaises(ValidationError):
            snap.save()
        with self.assertRaises(ProtectedError):
            self.quote.delete()

    def test_no_sellado_directo(self):
        draft = self.draft()
        draft.status = "ISSUED"
        with self.assertRaises(ValidationError):
            draft.save()

    def test_autorizacion_propietario_staff_y_otro(self):
        draft = self.draft()
        for action in [lambda: self.draft(actor=self.other),
                       lambda: emitir_revision(draft.pk, actor=self.other, expected_version=1),
                       lambda: obtener_documento(self.quote.folio, actor=self.other)]:
            with self.assertRaises(PermissionDenied):
                action()
        self.assertTrue(obtener_documento(self.quote.folio, actor=self.staff)[1].startswith(b"%PDF-"))

    def test_inactivo_y_anonimo_se_rechazan_en_servicios(self):
        self.actor.is_active = False
        for actor in [self.actor, AnonymousUser(), None]:
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.draft(actor=actor)

    def test_copia_de_inputs_no_deja_referencias_mutables(self):
        company = COMPANY.copy()
        conditions = ["Condición original"]
        draft = self.draft(company=company, conditions=conditions)
        company["name"] = "Otra"
        conditions.append("Otra")
        draft.refresh_from_db()
        self.assertEqual(draft.header["company"]["name"], COMPANY["name"])
        self.assertEqual(draft.header["conditions"], ["Condición original"])

    def test_renderer_falla_sin_emision_ni_archivos(self):
        draft = self.draft()
        with patch("apps.quotes.pdf.generate_quote_pdf", side_effect=RuntimeError("Prueba")), self.assertRaises(RuntimeError):
            self.issue(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "DRAFT")
        self.assertFalse(self.files())

    def test_renderer_resultado_invalido_no_archiva(self):
        draft = self.draft()
        with patch("apps.quotes.pdf.generate_quote_pdf", return_value=b"no pdf"), self.assertRaises(ValidationError):
            self.issue(draft)
        self.assertFalse(self.files())

    def test_escritura_corrupta_no_declara_emision_exitosa(self):
        draft = self.draft()
        storage = QuoteRevision._meta.get_field("pdf").storage
        real_save = storage.save

        def corrupt_save(name, content, **kwargs):
            saved = real_save(name, content, **kwargs)
            Path(storage.path(saved)).write_bytes(b"archivo truncado")
            return saved

        with patch.object(storage, "save", side_effect=corrupt_save), self.assertRaises(ArchivedPDFError):
            self.issue(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "DRAFT")
        self.assertFalse(self.files())

    def test_estado_comercial_aceptado_no_se_retrocede_al_emitir_revision(self):
        self.quote.status = Quote.Status.ACCEPTED
        self.quote.save()
        self.issue()
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)

    def test_storage_falla_sin_emision(self):
        draft = self.draft()
        storage = QuoteRevision._meta.get_field("pdf").storage
        with patch.object(storage, "save", side_effect=OSError("Prueba")), self.assertRaises(OSError):
            self.issue(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "DRAFT")
        self.assertIsNone(draft.snapshots.get().amount)
        self.assertFalse(self.files())

    def test_fallo_despues_de_guardar_limpia_solo_archivo_propio(self):
        first = self.issue()
        original = leer_pdf_emitido(first)
        second = self.draft()
        with patch.object(QuoteRevision, "save", side_effect=RuntimeError("Fallo DB")), self.assertRaises(RuntimeError):
            self.issue(second)
        second.refresh_from_db()
        self.assertEqual(second.status, "DRAFT")
        self.assertIsNone(second.snapshots.get().amount)
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(leer_pdf_emitido(first), original)

    def test_no_emite_dentro_de_atomic_real(self):
        draft = self.draft()
        with transaction.atomic(), self.assertRaises(RuntimeError):
            self.issue(draft)
        self.assertFalse(self.files())

    def test_archivo_ausente_o_alterado_no_se_regenera(self):
        issued = self.issue()
        path = Path(issued.pdf.path)
        path.write_bytes(b"alterado")
        with patch("apps.quotes.pdf.generate_quote_pdf", side_effect=AssertionError("No regenerar")), self.assertRaises(ArchivedPDFError):
            obtener_documento(self.quote.folio, actor=self.actor)
        path.unlink()
        with self.assertRaises(ArchivedPDFError):
            self.issue(issued)
        issued.refresh_from_db()
        self.assertEqual(issued.status, "ISSUED")

    def test_storage_no_tiene_url_publica(self):
        issued = self.issue()
        with self.assertRaises(ValueError):
            _ = issued.pdf.url

    def test_renderer_escapa_marcado_y_soporta_paginas(self):
        parts = [partida(description="Texto largo & <tag> " * 100) for _ in range(12)]
        draft = self.draft(parts, company={**COMPANY, "name": "Prueba & <b>Literal</b>"})
        context = contexto_revision(draft)
        data = generate_quote_pdf(context)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(context["company"]["name"], "Prueba & <b>Literal</b>")
        self.assertGreater(data.count(b"/Type /Page"), 2)

    def test_unitario_largo_en_detalle_no_se_trunca(self):
        draft = self.draft([partida(precio="382.533200", moneda="USD")],
                           tipo_cambio=tipo_cambio_manual("17.12345678", fecha=FECHA))
        context = contexto_revision(draft)
        from reportlab.platypus import Paragraph

        with patch("apps.quotes.pdf.Paragraph", wraps=Paragraph) as paragraph:
            generate_quote_pdf(context)
        textos = [call.args[0] for call in paragraph.call_args_list]
        expected = unit_money(context["items"][0]["unit_price"])
        self.assertIn("Ver detalle", textos)
        self.assertTrue(any(f"Unitario MXN: {expected}" in text for text in textos))

    def test_sin_red_y_contexto_decimal_local(self):
        draft = self.draft()
        with localcontext() as ctx, patch("socket.socket", side_effect=AssertionError("No red")):
            ctx.prec = 2
            ctx.traps[Inexact] = True
            ctx.traps[Rounded] = True
            before = ctx.copy()
            context = contexto_revision(draft)
            self.assertEqual(context["subtotal"], Decimal("10.04"))
            self.assertEqual((ctx.prec, ctx.flags, ctx.traps), (before.prec, before.flags, before.traps))


class DocumentViewsTests(SnapshotFixtures, TestCase):
    def url(self, folio=None):
        return f"/cotizaciones/{folio or self.quote.folio}/pdf/"

    def test_login_propiedad_y_admin(self):
        self.draft()
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_descarga_y_correo_mismos_bytes_archivados(self):
        issued = self.issue()
        expected = leer_pdf_emitido(issued)
        self.client.force_login(self.actor)
        with patch("apps.quotes.pdf.generate_quote_pdf", side_effect=AssertionError("No regenerar")):
            response = self.client.get(self.url())
            sent = self.client.post(f"/api/cotizaciones/{self.quote.folio}/enviar-correo/",
                                    json.dumps({"email": "destino@example.invalid"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, expected)
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(mail.outbox[0].attachments[0][1], expected)
        self.assertIn("$11.65 MXN", mail.outbox[0].alternatives[0].content)
        self.assertIn("Revisión 1", mail.outbox[0].alternatives[0].content)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_seleccion_revision_y_preferencia_ultima_emitida(self):
        first = self.issue()
        second = crear_revision(first.pk, actor=self.actor)
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(self.url()).content, leer_pdf_emitido(first))
        self.assertEqual(self.client.get(self.url()+"?revision=2").status_code, 200)
        self.assertEqual(self.client.get(self.url()+"?revision=99").status_code, 404)
        self.assertEqual(self.client.get(self.url()+"?revision=texto").status_code, 400)
        self.assertEqual(self.client.get(self.url()+"?revision="+"9"*5000).status_code, 400)
        self.issue(second)
        self.assertEqual(self.client.get(self.url()+"?revision=1").content, leer_pdf_emitido(first))

    def test_borrador_no_se_envia_implicitamente(self):
        self.draft()
        self.client.force_login(self.actor)
        response = self.client.post(f"/api/cotizaciones/{self.quote.folio}/enviar-correo/",
                                    json.dumps({"email": "destino@example.invalid"}), content_type="application/json")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.files())

    def test_legacy_sin_inventar_moneda_y_folio_ausente(self):
        QuoteItem.objects.create(quote=self.quote, description="Histórico", unit_price=Decimal("85.50"))
        context = get_quote_context(self.quote.folio)
        self.assertTrue(context["legacy_unknown_currency"])
        self.assertEqual(context["subtotal"], Decimal("85.50"))
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        self.assertEqual(self.client.get(self.url("NO-EXISTE")).status_code, 404)
        self.assertEqual(QuoteRevision.objects.count(), 0)

    def test_db_o_archivo_falla_sin_demo(self):
        self.client.force_login(self.actor)
        with patch("apps.quotes.views.obtener_documento", side_effect=RuntimeError("Prueba")), self.assertLogs("apps.quotes.views", level="ERROR"):
            self.assertEqual(self.client.get(self.url()).status_code, 500)
        issued = self.issue()
        Path(issued.pdf.path).unlink()
        self.assertEqual(self.client.get(self.url()).status_code, 503)

    def test_demo_solo_explicito_y_debug(self):
        demo_url = "/demo/cotizaciones/DEMO/pdf/"
        with override_settings(DEBUG=True):
            self.assertEqual(self.client.get(demo_url).status_code, 200)
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.get(demo_url).status_code, 404)
        context = build_demo_quote("DEMO")
        self.assertIn("DEMO", context["document_label"])
        self.assertEqual(context["subtotal"], sum(i["amount"] for i in context["items"]))


class SnapshotConcurrencyTests(SnapshotFixtures, TransactionTestCase):
    def run_concurrent(self, operation):
        if connection.vendor != "postgresql":
            self.skipTest("Bloqueos reales requieren PostgreSQL.")
        barrier = Barrier(2)

        def run():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.actor.pk)
                barrier.wait(timeout=10)
                return operation(actor)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            return [f.result(timeout=30) for f in futures]

    def test_creaciones_concurrentes_numero_e_idempotencia(self):
        key = uuid4()
        outputs = self.run_concurrent(lambda actor: self.draft(actor=actor, request_id=key).pk)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(QuoteRevision.objects.count(), 1)
        self.run_concurrent(lambda actor: self.draft(actor=actor).pk)
        self.assertEqual(list(self.quote.revisions.values_list("number", flat=True)), [1, 2, 3])

    def test_ediciones_concurrentes_solo_una_gana(self):
        draft = self.draft()

        def edit(actor):
            try:
                return self.edit(draft, actor=actor).edit_version
            except RevisionConflict:
                return "conflicto"

        self.assertCountEqual(self.run_concurrent(edit), [2, "conflicto"])
        draft.refresh_from_db()
        self.assertEqual(draft.edit_version, 2)
        self.assertEqual(draft.snapshots.count(), 1)

    def test_commit_falla_y_conserva_archivo_por_resultado_incierto(self):
        draft = self.draft()
        with patch.object(connection, "_commit", side_effect=DatabaseError("Commit de prueba")), self.assertLogs("apps.quotes.snapshots", level="ERROR"), self.assertRaises(DatabaseError):
            self.issue(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "DRAFT")
        self.assertIsNone(draft.snapshots.get().amount)
        self.assertEqual(len(self.files()), 1)

    def test_commit_real_con_respuesta_perdida_no_borra_original(self):
        draft = self.draft()
        real_commit = connection._commit

        def respuesta_perdida():
            real_commit()
            raise DatabaseError("Resultado del commit desconocido")

        with patch.object(connection, "_commit", side_effect=respuesta_perdida), self.assertLogs("apps.quotes.snapshots", level="ERROR"), self.assertRaises(DatabaseError):
            self.issue(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "ISSUED")
        self.assertTrue(leer_pdf_emitido(draft).startswith(b"%PDF-"))
        self.assertEqual(self.issue(draft).pk, draft.pk)
        self.assertEqual(len(self.files()), 1)

    def test_emisiones_concurrentes_mismo_documento(self):
        if connection.vendor != "postgresql":
            self.skipTest("Bloqueos reales requieren PostgreSQL.")
        draft = self.draft()
        barrier = Barrier(2)

        def emitir():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.actor.pk)
                barrier.wait(timeout=10)
                issued = emitir_revision(draft.pk, actor=actor, expected_version=1)
                return issued.pdf.name, issued.pdf_sha256
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(emitir) for _ in range(2)]
            outputs = [f.result(timeout=30) for f in futures]
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(len(self.files()), 1)

    async def test_servicios_async_fuera_del_event_loop(self):
        draft = await acrear_borrador(self.quote.pk, [partida()], actor=self.actor,
                                     company=COMPANY, conditions=["Prueba"])
        issued = await aemitir_revision(draft.pk, actor=self.actor, expected_version=1)
        _, data, _ = await aobtener_documento(self.quote.folio, actor=self.actor)
        self.assertEqual(hashlib.sha256(data).hexdigest(), issued.pdf_sha256)


class SnapshotMigrationTests(TransactionTestCase):
    def test_migracion_aditiva_conserva_legacy_sin_inferir_origen(self):
        executor = MigrationExecutor(connection)
        old_target = [("quotes", "0001_initial")]
        new_target = [("quotes", "0002_quoterevision_quotesnapshot")]
        try:
            executor.migrate(old_target)
            apps = executor.loader.project_state(old_target).apps
            user = get_user_model().objects.create_user(username="legacy_migracion")
            quote = apps.get_model("quotes", "Quote").objects.create(
                folio="LEGACY-M8", advisor_id=user.pk, client_name="Histórico", status="ISSUED")
            item = apps.get_model("quotes", "QuoteItem").objects.create(
                quote_id=quote.pk, description="Partida guardada", quantity=Decimal("3.50"), unit_price=Decimal("85.50"))
            MigrationExecutor(connection).migrate(new_target)
            current = Quote.objects.get(pk=quote.pk)
            saved = QuoteItem.objects.get(pk=item.pk)
            self.assertEqual((current.folio, current.status), ("LEGACY-M8", "ISSUED"))
            self.assertEqual((saved.description, saved.quantity, saved.unit_price), ("Partida guardada", Decimal("3.50"), Decimal("85.50")))
            self.assertFalse(current.revisions.exists())
            self.assertFalse(QuoteSnapshot.objects.exists())
        finally:
            MigrationExecutor(connection).migrate(new_target)
