"""P2/P3: persistencia, preview, emisión y migración v2 aislados, sin APIs reales."""

from dataclasses import replace
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal, Inexact, Rounded, localcontext
import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core import mail
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.quotes.models import Quote, QuoteCommercialConfirmation, QuoteRevision, QuoteSnapshot
from apps.quotes.permissions import ADJUSTMENTS, configure_commercial_groups
from apps.quotes.snapshots import (
    PartidaComercialCapturada, RevisionConflict, _commercial_stored,
    _current_commercial_state,
    ArchivedPDFError, aemitir_revision, aprevisualizar_comercial, aprevisualizar_revision_comercial,
    confirmar_advertencias_comerciales, crear_borrador_comercial, crear_revision_comercial,
    emitir_revision, guardar_borrador, guardar_borrador_comercial, validar_confirmaciones_comerciales,
    leer_pdf_emitido, obtener_documento, previsualizar_comercial, previsualizar_edicion_comercial,
    previsualizar_revision_comercial,
)
from apps.quotes.pdf import commercial_unit_money, generate_quote_pdf
from apps.quotes.tests import test_snapshots as snapshot_tests
from apps.quotes.tests.test_snapshots import COMPANY, SnapshotFixtures, partida
from apps.quotes.tests.test_commercial import fx

D = Decimal


def commercial_line(cost="100", quantity="1", margin="35", sale=None, kind="MANUAL", key=None, **kwargs):
    datos = dict(name="Concepto sintético", source_unit_price=None if cost is None else D(cost),
        source_currency=None if cost is None else "MXN", quantity=D(quantity), line_key=key or uuid4(),
        source_kind=kind, source_unit_label="Pieza", sale_unit_label="Pieza",
        pricing_mode="MARGIN" if sale is None else "MANUAL", margin_percent=D(margin) if sale is None else None,
        manual_unit_price=None if sale is None else D(sale), manual_currency=None if sale is None else "MXN")
    if kind == "API":
        datos.update(provider_code="qa", provider_name="Proveedor sintético", external_id="QA-1")
    datos.update(kwargs)
    return PartidaComercialCapturada(**datos)


class CommercialFixtures(SnapshotFixtures):
    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_user(username="bootstrap_qa", is_superuser=True)
        self.internal, self.commercial_admin = configure_commercial_groups(actor=self.root)
        self.actor.groups.add(self.internal, self.commercial_admin)
        self.other.groups.add(self.internal)
        self.staff.groups.add(self.internal)

    def commercial_draft(self, parts=None, **kwargs):
        datos = dict(actor=self.actor, company=COMPANY, conditions=["QA sin validez comercial."])
        datos.update(kwargs)
        return crear_borrador_comercial(self.quote.pk, [commercial_line()] if parts is None else parts, **datos)

    def commercial_edit(self, revision, parts=None, *, preview=False, **kwargs):
        datos = dict(actor=self.actor, expected_version=revision.edit_version,
            company=revision.header["company"], conditions=revision.header["conditions"], client=revision.header["client"],
            tipo_cambio=None, exchange_context_id=None, tax_rate=revision.tax_rate,
            discount_percent=revision.discount_percent, issue_date=revision.issue_date, valid_days=revision.valid_days)
        datos.update(kwargs)
        parts = [PartidaComercialCapturada(**p) for p in _commercial_stored(revision)] if parts is None else parts
        service = previsualizar_edicion_comercial if preview else guardar_borrador_comercial
        return service(revision.pk, parts, **datos)

    def confirm(self, revision, warnings, **kwargs):
        datos = dict(actor=self.actor, expected_version=revision.edit_version, state_fingerprint=revision.state_fingerprint)
        datos.update(kwargs)
        return confirmar_advertencias_comerciales(revision.pk, warnings, **datos)


class CommercialPersistenceTests(CommercialFixtures, TestCase):
    def test_borrador_v2_captura_sin_sellar_ni_tocar_legacy(self):
        r = self.commercial_draft()
        self.assertEqual((r.schema_version, r.calculation_version, r.discount_percent), (2, 1, D("0")))
        self.assertEqual(len(r.state_fingerprint), 64)
        self.assertEqual(r.state_fingerprint, r.request_fingerprint)
        self.assertIsNone(r.total)
        self.assertIsNone(r.sale_subtotal)
        self.assertIsNone(r.gross_profit)
        s = r.snapshots.get()
        self.assertEqual((s.source_unit_price, s.source_currency, s.margin_percent), (D("100"), "MXN", D("35")))
        self.assertEqual(s.unit_validated_by, self.actor)
        self.assertIsNone(s.sale_amount)
        self.assertIsNone(s.amount)
        self.assertFalse(self.quote.items.exists())
        self.assertFalse(self.files())

    def test_costo_ausente_no_se_convierte_en_cero_y_venta_manual_calcula(self):
        r = self.commercial_draft([commercial_line(cost=None, sale="200")])
        s = r.snapshots.get()
        self.assertIsNone(s.source_unit_price)
        self.assertIsNone(s.source_currency)
        self.assertEqual(s.manual_unit_price, D("200"))
        self.confirm(r, [("COSTO_DESCONOCIDO", str(s.line_key))])
        result = validar_confirmaciones_comerciales(r.pk, actor=self.actor, expected_version=1)
        self.assertEqual(result.total, D("232"))
        self.assertIsNone(result.utilidad_bruta)

    def test_costo_cero_conocido_y_venta_cero_tienen_estado_distinto(self):
        r = self.commercial_draft([commercial_line(cost="0", sale="0")])
        s = r.snapshots.get()
        self.assertEqual(s.source_unit_price, D("0"))
        self.assertEqual(s.source_currency, "MXN")
        self.confirm(r, [("VENTA_CERO", str(s.line_key)), ("TOTAL_CERO", "")])
        self.assertEqual(validar_confirmaciones_comerciales(r.pk, actor=self.actor, expected_version=1).utilidad_bruta, D("0"))

    def test_descuento_global_no_guarda_reparto(self):
        r = self.commercial_draft([commercial_line(sale="600", cost="580"), commercial_line(sale="400", cost="200")],
                                  discount_percent=D("10"))
        result = validar_confirmaciones_comerciales(r.pk, actor=self.actor, expected_version=1)
        self.assertEqual((result.subtotal_neto, result.utilidad_bruta), (D("900"), D("120")))
        self.assertEqual(result.advertencias, ())
        self.assertIsNone(r.discount_amount)

    def test_reintento_idempotente_y_otro_payload_conflicto(self):
        parts, request_id = [commercial_line()], uuid4()
        r = self.commercial_draft(parts, request_id=request_id)
        again = self.commercial_draft(parts, request_id=request_id)
        self.assertEqual((r.pk, r.state_fingerprint), (again.pk, again.state_fingerprint))
        with self.assertRaises(RevisionConflict):
            self.commercial_draft([replace(parts[0], quantity=D("2"))], request_id=request_id)
        self.assertEqual(self.quote.revisions.count(), 1)

    def test_editar_conserva_captura_y_renueva_huella_no_huella_de_creacion(self):
        r = self.commercial_draft([commercial_line(kind="API")])
        s = r.snapshots.get()
        initial = r.request_fingerprint
        self.commercial_edit(r, [replace(PartidaComercialCapturada(**_commercial_stored(r)[0]), quantity=D("2"))])
        r.refresh_from_db()
        after = r.snapshots.get()
        self.assertEqual(r.edit_version, 2)
        self.assertEqual(r.request_fingerprint, initial)
        self.assertNotEqual(r.state_fingerprint, initial)
        self.assertEqual((after.line_key, after.captured_at, after.unit_validated_at), (s.line_key, s.captured_at, s.unit_validated_at))
        self.assertEqual(after.source_unit_price, s.source_unit_price)

    def test_api_original_no_se_sustituye_por_editar(self):
        p = commercial_line(kind="API")
        r = self.commercial_draft([p])
        with self.assertRaises(RevisionConflict):
            self.commercial_edit(r, [replace(p, source_unit_price=D("1"))])
        self.assertEqual(r.snapshots.get().source_unit_price, D("100"))

    def test_unidad_fraccionada_validada_por_actor_y_cantidad_entera(self):
        p = commercial_line(cost="3050", quantity="20", source_unit_label="Bobina", sale_unit_label="Metro", conversion_factor=D("305"))
        r = self.commercial_draft([p])
        result = validar_confirmaciones_comerciales(r.pk, actor=self.actor, expected_version=1)
        self.assertEqual(result.subtotal_venta, D("307.69"))
        with self.assertRaises(ValidationError):
            self.commercial_edit(r, [replace(p, quantity=D("1.5"))])

    def test_validaciones_no_dejan_revision_parcial(self):
        invalid = [commercial_line(cost=None), commercial_line(margin="100"),
            commercial_line(quantity="0"), commercial_line(kind="API", cost=None, sale="20"),
            commercial_line(source_unit_label=""), commercial_line(provider_code="api", external_id="1", provider_name="API"),
            commercial_line(cost=None, sale="20", source_currency="MXN"), commercial_line(cost="1.0000001")]
        for p in invalid:
            with self.subTest(p=p), self.assertRaises((ValidationError, TypeError)):
                self.commercial_draft([p])
        self.assertFalse(self.quote.revisions.exists())

    def test_doble_identidad_y_version_obsoleta_se_rechazan(self):
        p = commercial_line()
        with self.assertRaises(ValidationError):
            self.commercial_draft([p, p])
        r = self.commercial_draft([p])
        self.commercial_edit(r)
        with self.assertRaises(RevisionConflict):
            self.commercial_edit(r)

    def test_fx_con_otro_desfase_se_reconstruye_con_la_misma_huella(self):
        tipo = fx()
        tipo = replace(tipo, fecha=tipo.fecha.astimezone(dt_timezone(timedelta(hours=-6))))
        r = self.commercial_draft([commercial_line(kind="API", source_currency="USD")],
            tipo_cambio=tipo, exchange_context_id=uuid4())
        r.refresh_from_db()
        result = validar_confirmaciones_comerciales(r.pk, actor=self.actor, expected_version=1)
        self.assertEqual(result.costo_conocido, D("1750.00"))

    def test_constancia_inmutable_e_idempotente_y_obsoleta_tras_editar(self):
        r = self.commercial_draft([commercial_line(margin="0")])
        warning = [("MARGEN_CERO", str(r.snapshots.get().line_key))]
        a = self.confirm(r, warning)[0]
        self.assertEqual(a.pk, self.confirm(r, warning)[0].pk)
        with self.assertRaises(ValidationError):
            a.save()
        with self.assertRaises(ValidationError):
            a.delete()
        with self.assertRaises(ValidationError):
            QuoteCommercialConfirmation.objects.all().update(warning_code="TOTAL_CERO")
        self.commercial_edit(r, conditions=["Condición nueva."])
        r.refresh_from_db()
        self.assertTrue(QuoteCommercialConfirmation.objects.filter(pk=a.pk).exists())
        with self.assertRaises(PermissionDenied):
            validar_confirmaciones_comerciales(r.pk, actor=self.actor, expected_version=2)
        with self.assertRaises(RevisionConflict):
            self.confirm(r, warning, state_fingerprint=a.state_fingerprint)

    def test_confirmar_codigo_o_partida_ajenos_no_es_valido(self):
        r = self.commercial_draft([commercial_line(margin="0")])
        for warning in [("INVENTADO", ""), ("MARGEN_CERO", str(uuid4())), ("TOTAL_CERO", "")]:
            with self.subTest(warning=warning), self.assertRaises(ValidationError):
                self.confirm(r, [warning])
        self.assertFalse(QuoteCommercialConfirmation.objects.exists())

    def test_v1_conserva_decimales_y_transicion_es_otra_revision_explicita(self):
        original = self.draft([partida(cantidad="1.50")])
        new = crear_revision_comercial(original.pk, [commercial_line()], actor=self.actor,
            request_id=uuid4(), tax_rate=D("0.16"), discount_percent=D("0"), tipo_cambio=None, exchange_context_id=None)
        original.refresh_from_db()
        self.assertEqual((original.schema_version, original.snapshots.get().quantity), (1, D("1.50")))
        self.assertEqual((new.schema_version, new.based_on_id, new.number), (2, original.pk, 2))
        self.assertFalse(new.commercial_confirmations.exists())

    def test_servicio_edicion_v1_no_puede_interpretar_v2(self):
        r = self.commercial_draft()
        with self.assertRaises(RevisionConflict):
            self.edit(r)
        self.assertFalse(self.files())
        r.refresh_from_db()
        self.assertEqual(r.status, "DRAFT")

    def test_borrador_v2_se_edita_no_se_clona_como_si_fuera_emitido(self):
        p = commercial_line(kind="API")
        r = self.commercial_draft([p])
        with self.assertRaises(RevisionConflict):
            crear_revision_comercial(r.pk, [p], actor=self.actor, request_id=uuid4(),
                tax_rate=r.tax_rate, discount_percent=r.discount_percent, tipo_cambio=None, exchange_context_id=None)

    def test_modelo_emitido_v2_inmutable_y_clon_conserva_ajuste_sin_permiso_nuevo(self):
        # Fixture de sellado de MODELO, no emisión/PDF comercial implementados.
        p = commercial_line(kind="API", margin="40")
        r = self.commercial_draft([p])
        result = _current_commercial_state(r)
        s = r.snapshots.get()
        s.sale_amount, s.cost_amount = result.partidas[0].importe_venta, result.partidas[0].importe_costo
        s.save(_seal=True)
        r.sale_subtotal, r.discount_amount = result.subtotal_venta, result.descuento
        r.subtotal, r.tax, r.total = result.subtotal_neto, result.iva, result.total
        r.known_cost_total, r.costs_complete, r.gross_profit = result.costo_conocido, True, result.utilidad_bruta
        r.pdf.name, r.pdf_sha256, r.issued_at = "fixture-modelo-no-documento.pdf", "a" * 64, timezone.now()
        r.status = "ISSUED"
        r.save(_seal=True)
        with self.assertRaises(ValidationError):
            r.save()
        with self.assertRaises(ValidationError):
            s.save()
        self.actor.groups.remove(self.commercial_admin)
        child = crear_revision_comercial(r.pk, [p], actor=self.actor, request_id=uuid4(),
            tax_rate=r.tax_rate, discount_percent=r.discount_percent, tipo_cambio=None, exchange_context_id=None)
        self.assertEqual((child.status, child.snapshots.get().margin_percent), ("DRAFT", D("40")))
        self.assertIsNone(child.total)
        self.assertFalse(child.pdf.name)
        self.assertFalse(child.commercial_confirmations.exists())

    def test_constraints_sql_rechazan_combinaciones_imposibles(self):
        r = self.commercial_draft()
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("UPDATE quotes_quotesnapshot SET pricing_mode = NULL WHERE revision_id = %s", [r.pk])
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("UPDATE quotes_quotesnapshot SET quantity = 1.5 WHERE revision_id = %s", [r.pk])
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("UPDATE quotes_quoterevision SET discount_percent = 101 WHERE id = %s", [r.pk])
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("UPDATE quotes_quotesnapshot SET source_kind = NULL WHERE revision_id = %s", [r.pk])
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("UPDATE quotes_quoterevision SET calculation_version = NULL WHERE id = %s", [r.pk])


class CommercialServicesTests(CommercialFixtures, TestCase):
    def preview(self, revision):
        return previsualizar_revision_comercial(revision.pk, actor=self.actor, expected_version=revision.edit_version)

    def test_preview_sin_guardar_no_crea_revision_constancia_pdf_ni_toca_quote(self):
        before = Quote.objects.values().get(pk=self.quote.pk)
        p = commercial_line(cost=None, sale="100")
        with CaptureQueriesContext(connection) as queries:
            output = previsualizar_comercial(self.quote.pk, [p], actor=self.actor, company=COMPANY, conditions=["QA"])
        for query in queries.captured_queries:
            self.assertFalse(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")))
        self.assertEqual(output["cliente"]["total"], D("116"))
        self.assertEqual(output["resultado"].advertencias[0].codigo, "COSTO_DESCONOCIDO")
        self.assertIsNone(output["edit_version"])
        self.assertIsNone(output["cliente"]["revision"])
        self.assertFalse(QuoteRevision.objects.exists())
        self.assertFalse(QuoteCommercialConfirmation.objects.exists())
        self.assertFalse(self.files())
        self.assertEqual(Quote.objects.values().get(pk=self.quote.pk), before)

    def test_preview_sin_guardar_y_guardado_tienen_mismos_valores_y_huella(self):
        p = commercial_line(quantity="3", source_currency="USD", cost_tax_rate_included=D(".16"))
        kwargs = dict(actor=self.actor, company=COMPANY, conditions=["QA"], tipo_cambio=fx(),
                      exchange_context_id=uuid4(), discount_percent=D("7.25"))
        unsaved = previsualizar_comercial(self.quote.pk, [p], **kwargs)
        r = crear_borrador_comercial(self.quote.pk, [p], **kwargs)
        saved = self.preview(r)
        self.assertEqual(unsaved["resultado"], saved["resultado"])
        self.assertEqual(unsaved["state_fingerprint"], saved["state_fingerprint"])
        self.assertEqual(unsaved["cliente"]["items"], saved["cliente"]["items"])

    def test_preview_valida_origen_modo_y_unidades_igual_que_guardar(self):
        for p in (commercial_line(kind="API", provider_code=""), commercial_line(source_kind="OTRO"),
                  commercial_line(pricing_mode="MANUAL"), commercial_line(sale_unit_label=""),
                  commercial_line(kind="API", provider_name="")):
            with self.subTest(p=p), self.assertRaises(ValidationError):
                previsualizar_comercial(self.quote.pk, [p], actor=self.actor, company=COMPANY, conditions=["QA"])
        self.assertFalse(QuoteRevision.objects.exists())

    def test_preview_guardado_con_avisos_no_sella_y_version_obsoleta_no_pasa(self):
        r = self.commercial_draft([commercial_line(margin="0")])
        output = self.preview(r)
        self.assertEqual(output["resultado"].total, D("116"))
        self.assertEqual((output["edit_version"], output["state_fingerprint"]), (1, r.state_fingerprint))
        r.refresh_from_db()
        self.assertIsNone(r.subtotal)
        self.assertIsNone(r.snapshots.get().sale_amount)
        self.assertFalse(self.files())
        self.commercial_edit(r)
        with self.assertRaises(RevisionConflict):
            self.preview(r)

    def test_edicion_simulada_no_guarda_y_su_huella_coincide_al_guardar(self):
        p = commercial_line(margin="0")
        r = self.commercial_draft([p])
        candidate = [replace(p, quantity=D("2"))]
        output = self.commercial_edit(r, candidate, preview=True)
        self.assertFalse(output["persistida"])
        self.assertEqual((output["edit_version"], output["base_state_fingerprint"]), (1, r.state_fingerprint))
        self.assertNotEqual(output["state_fingerprint"], r.state_fingerprint)
        self.assertEqual(r.snapshots.get().quantity, D("1"))
        self.assertFalse(self.files())
        with self.assertRaises(RevisionConflict):
            self.confirm(r, [("MARGEN_CERO", str(p.line_key))], state_fingerprint=output["state_fingerprint"])
        saved = self.commercial_edit(r, candidate)
        self.assertEqual(saved.state_fingerprint, output["state_fingerprint"])
        self.assertEqual(self.preview(saved)["resultado"], output["resultado"])

    def test_simular_edicion_no_recaptura_original_api(self):
        p = commercial_line(kind="API")
        r = self.commercial_draft([p])
        with self.assertRaises(RevisionConflict):
            self.commercial_edit(r, [replace(p, source_unit_price=D("200"))], preview=True)

    def test_preview_y_emision_tienen_mismos_importes_y_sellado_completo(self):
        r = self.commercial_draft([commercial_line(kind="API", quantity="3")], discount_percent=D("10"))
        output = self.preview(r)
        issued = self.issue(r)
        result = output["resultado"]
        self.assertEqual((issued.sale_subtotal, issued.discount_amount, issued.subtotal, issued.tax, issued.total),
            (result.subtotal_venta, result.descuento, result.subtotal_neto, result.iva, result.total))
        self.assertEqual((issued.known_cost_total, issued.costs_complete, issued.gross_profit),
            (result.costo_conocido, True, result.utilidad_bruta))
        s = issued.snapshots.get()
        self.assertEqual((s.amount, s.sale_amount, s.cost_amount), (None, result.partidas[0].importe_venta, D("300")))
        self.assertEqual((s.source_unit_price, s.margin_percent, issued.state_fingerprint), (D("100"), D("35"), r.state_fingerprint))
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(obtener_documento(self.quote.folio, actor=self.actor)[0]["total"], result.total)
        with self.assertRaises(RevisionConflict):
            self.preview(issued)

    def test_datos_internos_no_forman_parte_del_contexto_cliente_ni_pdf(self):
        p = commercial_line(kind="API", source_currency="USD", provider_name="DISTRIBUIDOR_SECRETO_QA",
            brand="MARCA_PUBLICA_QA", model="MODELO_PUBLICO_QA", external_id="ID_INTERNO_QA")
        r = self.commercial_draft([p], tipo_cambio=fx(), exchange_context_id=uuid4())
        preview = self.preview(r)
        client = preview["cliente"]
        for key in ("provider", "provider_name", "source_unit_price", "cost_amount", "margin_percent",
                    "gross_profit", "exchange_rate", "stock", "state_fingerprint", "external_id"):
            self.assertNotIn(key, client)
            self.assertNotIn(key, client["items"][0])
        self.assertNotIn("DISTRIBUIDOR_SECRETO_QA", str(client))
        self.assertNotIn("ID_INTERNO_QA", str(client))
        self.assertNotIn("USD", str(client))
        self.assertEqual(client["items"][0]["brand"], "MARCA_PUBLICA_QA")
        from reportlab.platypus import Paragraph
        with patch("apps.quotes.pdf.Paragraph", wraps=Paragraph) as paragraphs:
            data = generate_quote_pdf(client)
        text = "\n".join(c.args[0] for c in paragraphs.call_args_list)
        for secret in ("DISTRIBUIDOR_SECRETO_QA", "ID_INTERNO_QA", "Origen:", "Stock capturado:", "1 USD", "Marca / Dist."):
            self.assertNotIn(secret, text)
        self.assertIn("MARCA_PUBLICA_QA", text)
        self.assertIn("Marca / Modelo", text)
        self.assertTrue(data.startswith(b"%PDF-"))

        # También verificar el PDF real descomprimido, no solo los Paragraph.
        from reportlab.platypus import SimpleDocTemplate
        def uncompressed(*args, **kwargs):
            return SimpleDocTemplate(*args, pageCompression=0, **kwargs)
        with patch("apps.quotes.pdf.SimpleDocTemplate", side_effect=uncompressed):
            pdf = generate_quote_pdf(client)
        self.assertTrue(b"MARCA_PU" in pdf)  # El texto público puede dividirse en renglones.
        self.assertFalse(b"DISTRIBUIDOR_SECRETO_QA" in pdf)
        self.assertFalse(b"ID_INTERNO_QA" in pdf)
        self.assertFalse(b"USD" in pdf)

    def test_unitario_periodico_leyenda_y_desglose_global_sin_reparto(self):
        p = commercial_line(cost="100", margin="0", quantity="3", conversion_factor=D("3"))
        r = self.commercial_draft([p], discount_percent=D("10"))
        context = self.preview(r)["cliente"]
        item = context["items"][0]
        self.assertEqual(item["amount"], D("100"))
        self.assertIn("$33.333", item["unit_price_label"])
        self.assertTrue(item["unit_price_approximate"])
        self.assertIn("aprox.", item["unit_price_label"])
        self.assertEqual((context["sale_subtotal"], context["discount_amount"], context["subtotal"]),
                         (D("100"), D("10"), D("90")))
        self.assertNotIn("discount_amount", item)
        from reportlab.platypus import Paragraph
        with patch("apps.quotes.pdf.Paragraph", wraps=Paragraph) as paragraphs:
            generate_quote_pdf(context)
        text = "\n".join(c.args[0] for c in paragraphs.call_args_list)
        self.assertIn("precisión completa", text)
        self.assertIn("Descuento global (10%)", text)
        self.assertIn("I.V.A. (16%)", text)
        # Presentación v2: aprox. en la celda, fecha de borrador y sin campos vacíos de empresa.
        self.assertIn("$33.333<br/>(aprox.)", text)
        self.assertNotIn("Ver detalle", text)
        self.assertIn("Fecha de propuesta:", text)
        self.assertNotIn("Fecha de Emisión", text)
        self.assertNotIn("RFC: <br/>", text)
        self.assertNotIn("Tel:", text)
        self.assertIn("Venta antes de descuento:", text)
        self.assertIn("<b>-$10.00 MXN</b>", text)

    def test_descuento_cero_conserva_renglon_sin_signo(self):
        context = self.preview(self.commercial_draft([commercial_line(cost="100", margin="0")]))["cliente"]
        self.assertEqual(context["discount_amount"], D("0"))
        from reportlab.platypus import Paragraph
        with patch("apps.quotes.pdf.Paragraph", wraps=Paragraph) as paragraphs:
            generate_quote_pdf(context)
        text = "\n".join(c.args[0] for c in paragraphs.call_args_list)
        self.assertIn("Descuento global (0%)", text)
        self.assertIn("<b>$0.00 MXN</b>", text)
        self.assertNotIn("-$0.00", text)

    def test_unitario_exacto_y_empate_periodico_no_cambian_importe(self):
        self.assertEqual(commercial_unit_money(D("10"), D("3"), D("30")), ("$10.00", False))
        p = commercial_line(cost=".025", margin="0", quantity="3", conversion_factor=D("3"))
        context = self.preview(self.commercial_draft([p]))["cliente"]
        self.assertEqual(context["items"][0]["amount"], D(".03"))
        self.assertTrue(context["items"][0]["unit_price_approximate"])

    def test_preview_no_depende_del_contexto_decimal_global(self):
        r = self.commercial_draft()
        with localcontext() as ctx:
            ctx.prec = 2
            ctx.traps[Inexact] = True
            ctx.traps[Rounded] = True
            before = ctx.copy()
            preview = self.preview(r)
            data = generate_quote_pdf(preview["cliente"])
            self.assertEqual(preview["cliente"]["total"], D("178.47"))
            self.assertEqual((ctx.prec, ctx.flags, ctx.traps), (before.prec, before.flags, before.traps))
        self.assertTrue(data.startswith(b"%PDF-"))

    def test_costo_desconocido_emite_con_aceptacion_y_utilidad_incompleta(self):
        p = commercial_line(cost=None, sale="100")
        r = self.commercial_draft([p])
        self.confirm(r, [("COSTO_DESCONOCIDO", str(p.line_key))])
        issued = self.issue(r)
        self.assertEqual((issued.known_cost_total, issued.costs_complete, issued.gross_profit), (D("0"), False, None))
        self.assertIsNone(issued.snapshots.get().cost_amount)
        self.assertEqual(issued.total, D("116"))

    def test_total_cero_y_perdida_global_requieren_todos_los_avisos(self):
        p = commercial_line(kind="API")
        r = self.commercial_draft([p], discount_percent=D("100"))
        with self.assertRaises(PermissionDenied):
            self.issue(r)
        self.confirm(r, [("TOTAL_CERO", "")])
        with self.assertRaises(PermissionDenied):
            self.issue(r)
        self.confirm(r, [("VENTA_BAJO_COSTO", "")])
        issued = self.issue(r)
        self.assertEqual((issued.total, issued.gross_profit), (D("0"), D("-100")))

    def test_cambiar_cliente_invalida_aceptacion_sin_borrar_historia(self):
        p = commercial_line(margin="0")
        r = self.commercial_draft([p])
        self.confirm(r, [("MARGEN_CERO", str(p.line_key))])
        r = self.commercial_edit(r, client={**r.header["client"], "business_name": "Otro cliente QA"})
        with patch("apps.quotes.pdf.generate_quote_pdf", side_effect=AssertionError("No debe renderizar")), self.assertRaises(PermissionDenied):
            self.issue(r)
        self.assertEqual(r.commercial_confirmations.count(), 1)
        self.assertFalse(self.files())

    def test_emision_revalida_permiso_revocado_no_usa_comprobacion_anterior(self):
        p = commercial_line(margin="0")
        r = self.commercial_draft([p])
        self.confirm(r, [("MARGEN_CERO", str(p.line_key))])
        validar_confirmaciones_comerciales(r.pk, actor=self.actor, expected_version=1)
        self.actor.groups.remove(self.commercial_admin)
        with self.assertRaises(PermissionDenied):
            self.issue(r)
        self.assertFalse(self.files())

    def test_huella_alterada_no_previsualiza_ni_emite(self):
        r = self.commercial_draft()
        with connection.cursor() as cur:
            cur.execute("UPDATE quotes_quotesnapshot SET quantity = 2 WHERE revision_id = %s", [r.pk])
        with self.assertRaises(RevisionConflict):
            self.preview(r)
        with self.assertRaises(RevisionConflict):
            self.issue(r)
        self.assertFalse(self.files())

    def test_reintento_y_documento_archivado_sin_render_ni_aceptacion_nueva(self):
        p = commercial_line(margin="0")
        r = self.commercial_draft([p])
        self.confirm(r, [("MARGEN_CERO", str(p.line_key))])
        issued = self.issue(r)
        expected = leer_pdf_emitido(issued)
        self.actor.groups.remove(self.commercial_admin)
        with patch("apps.quotes.pdf.generate_quote_pdf", side_effect=AssertionError("No regenerar")), patch("apps.quotes.snapshots._commercial_calculate", side_effect=AssertionError("No recalcular archivo")):
            self.assertEqual(self.issue(issued).pk, issued.pk)
            context, data, filename = obtener_documento(self.quote.folio, actor=self.actor, number=1)
        self.assertEqual(data, expected)
        self.assertNotIn("source_unit_price", str(context))
        self.assertEqual(hashlib.sha256(data).hexdigest(), issued.pdf_sha256)
        self.assertEqual(len(self.files()), 1)
        self.assertTrue(context["archived_only"])
        with self.assertRaises(ValueError):
            generate_quote_pdf(context)

    def test_pdf_ausente_o_corrupto_no_se_regenera(self):
        r = self.issue(self.commercial_draft())
        path = Path(r.pdf.path)
        path.write_bytes(b"corrupto")
        with patch("apps.quotes.pdf.generate_quote_pdf", side_effect=AssertionError("No regenerar")):
            with self.assertRaises(ArchivedPDFError):
                obtener_documento(self.quote.folio, actor=self.actor)
            path.unlink()
            with self.assertRaises(ArchivedPDFError):
                self.issue(r)

    def test_sellado_inconsistente_se_detecta_sin_sobrescribir_original(self):
        r = self.issue(self.commercial_draft())
        original = leer_pdf_emitido(r)
        with connection.cursor() as cur:
            cur.execute("UPDATE quotes_quotesnapshot SET sale_amount = sale_amount + .01 WHERE revision_id = %s", [r.pk])
        with self.assertRaises(RevisionConflict):
            self.preview(r)
        with self.assertRaises(RevisionConflict):
            obtener_documento(self.quote.folio, actor=self.actor)
        self.assertEqual(leer_pdf_emitido(r), original)

    def test_fallos_renderer_storage_y_sello_revierten_solo_sus_archivos(self):
        first = self.issue(self.commercial_draft())
        expected = leer_pdf_emitido(first)
        r = self.commercial_draft()
        storage = QuoteRevision._meta.get_field("pdf").storage
        for target, effect in (("apps.quotes.pdf.generate_quote_pdf", RuntimeError("renderer QA")),
                               ("apps.quotes.models.QuoteRevision.save", RuntimeError("sello QA"))):
            with self.subTest(target=target), patch(target, side_effect=effect), self.assertRaises(RuntimeError):
                self.issue(r)
            r.refresh_from_db()
            self.assertEqual((r.status, r.total, r.snapshots.get().sale_amount), ("DRAFT", None, None))
            self.assertEqual(len(self.files()), 1)
        with patch.object(storage, "save", side_effect=OSError("storage QA")), self.assertRaises(OSError):
            self.issue(r)
        self.assertEqual(leer_pdf_emitido(first), expected)

    def test_corrupt_save_no_sella_y_limpia_pdf_propio(self):
        r = self.commercial_draft()
        storage = QuoteRevision._meta.get_field("pdf").storage
        real = storage.save
        def corrupt(*args, **kwargs):
            name = real(*args, **kwargs)
            Path(storage.path(name)).write_bytes(b"truncado")
            return name
        with patch.object(storage, "save", side_effect=corrupt), self.assertRaises(ArchivedPDFError):
            self.issue(r)
        self.assertFalse(self.files())
        r.refresh_from_db()
        self.assertIsNone(r.known_cost_total)

    def test_render_multipagina_escapa_texto_y_unitario_largo_sin_origen(self):
        parts = [commercial_line(sale="999999999999.123456", description="Largo & <b>literal</b> " * 100) for _ in range(15)]
        context = self.preview(self.commercial_draft(parts))["cliente"]
        data = generate_quote_pdf(context)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreater(data.count(b"/Type /Page"), 2)
        self.assertIn("<b>literal</b>", context["items"][0]["description"])

    def test_documento_borrador_no_archiva_y_no_se_envia_por_correo(self):
        r = self.commercial_draft()
        context, data, name = obtener_documento(self.quote.folio, actor=self.actor, number=r.number)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertTrue(context["commercial_client"])
        self.assertFalse(self.files())
        with self.assertRaises(RevisionConflict):
            obtener_documento(self.quote.folio, actor=self.actor, allow_draft=False)

    def test_descarga_y_correo_usan_mismos_bytes_y_solo_contexto_cliente(self):
        r = self.issue(self.commercial_draft())
        expected = leer_pdf_emitido(r)
        self.client.force_login(self.actor)
        with patch("apps.quotes.pdf.generate_quote_pdf", side_effect=AssertionError("No regenerar")):
            download = self.client.get(f"/cotizaciones/{self.quote.folio}/pdf/?revision=1")
            sent = self.client.post(f"/api/cotizaciones/{self.quote.folio}/enviar-correo/",
                json.dumps({"email": "qa@example.invalid", "revision": 1}), content_type="application/json")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, expected)
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(mail.outbox[0].attachments[0][1], expected)
        self.assertNotIn("USD", mail.outbox[0].alternatives[0].content)

    def test_pdf_original_v1_no_cambia_al_emitir_v2(self):
        first = self.issue(self.draft())
        original = leer_pdf_emitido(first)
        second = crear_revision_comercial(first.pk, [commercial_line()], actor=self.actor,
            request_id=uuid4(), tax_rate=D(".16"), discount_percent=D("0"), tipo_cambio=None, exchange_context_id=None)
        self.issue(second)
        self.assertEqual(obtener_documento(self.quote.folio, actor=self.actor, number=1)[1], original)
        self.assertEqual(len(self.files()), 2)


class CommercialConcurrencyTests(CommercialFixtures, TransactionTestCase):
    run_concurrent = snapshot_tests.SnapshotConcurrencyTests.run_concurrent

    def test_emisiones_concurrentes_reutilizan_unico_original(self):
        r = self.commercial_draft()
        outputs = self.run_concurrent(lambda actor: emitir_revision(r.pk, actor=actor, expected_version=1).pdf_sha256)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(len(self.files()), 1)

    def test_commit_incierto_conserva_archivo_y_no_declara_emision(self):
        r = self.commercial_draft()
        with patch.object(connection, "_commit", side_effect=DatabaseError("commit QA")), self.assertLogs("apps.quotes.snapshots", level="ERROR"), self.assertRaises(DatabaseError):
            self.issue(r)
        r.refresh_from_db()
        self.assertEqual((r.status, r.sale_subtotal, r.snapshots.get().sale_amount), ("DRAFT", None, None))
        self.assertEqual(len(self.files()), 1)

    def test_commit_real_respuesta_perdida_reintenta_sin_regenerar(self):
        r = self.commercial_draft()
        real = connection._commit
        def lost():
            real()
            raise DatabaseError("respuesta perdida QA")
        with patch.object(connection, "_commit", side_effect=lost), self.assertLogs("apps.quotes.snapshots", level="ERROR"), self.assertRaises(DatabaseError):
            self.issue(r)
        r.refresh_from_db()
        self.assertEqual(r.status, "ISSUED")
        with patch("apps.quotes.pdf.generate_quote_pdf", side_effect=AssertionError("No regenerar")):
            self.assertEqual(self.issue(r).pdf_sha256, r.pdf_sha256)
        self.assertEqual(len(self.files()), 1)

    def test_editar_y_emitir_en_paralelo_solo_uno_gana(self):
        r = self.commercial_draft()
        barrier = Barrier(2)
        def run(edit):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.actor.pk)
                barrier.wait(timeout=10)
                try:
                    return self.commercial_edit(r, actor=actor).status if edit else emitir_revision(r.pk, actor=actor, expected_version=1).status
                except RevisionConflict:
                    return "CONFLICT"
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, edit) for edit in (True, False)]
            outputs = [f.result(timeout=30) for f in futures]
        self.assertEqual(outputs.count("CONFLICT"), 1)
        r.refresh_from_db()
        self.assertEqual(len(self.files()), 1 if r.status == "ISSUED" else 0)

    def test_preview_concurrente_no_mezcla_cabecera_partidas_y_version(self):
        r = self.commercial_draft()
        barrier = Barrier(2)
        p = commercial_line(quantity="2")
        def run(edit):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.actor.pk)
                barrier.wait(timeout=10)
                if edit:
                    return self.commercial_edit(r, [p], actor=actor,
                        client={**r.header["client"], "business_name": "Cliente nuevo QA"}).edit_version
                try:
                    return previsualizar_revision_comercial(r.pk, actor=actor, expected_version=1)
                except RevisionConflict:
                    return "CONFLICT"
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, edit) for edit in (True, False)]
            edited, preview = [f.result(timeout=30) for f in futures]
        self.assertEqual(edited, 2)
        if preview != "CONFLICT":
            self.assertEqual(preview["edit_version"], 1)
            self.assertEqual(preview["cliente"]["client"]["business_name"], r.header["client"]["business_name"])
            self.assertEqual(preview["cliente"]["items"][0]["qty"], 1)

    def test_emision_mantiene_bloqueados_usuarios_hasta_commit(self):
        r = self.commercial_draft()
        real = generate_quote_pdf
        def probe():
            close_old_connections()
            try:
                with transaction.atomic():
                    get_user_model().objects.select_for_update(nowait=True).get(pk=self.actor.pk)
            except DatabaseError:
                return "LOCKED"
            finally:
                close_old_connections()
            return "UNLOCKED"
        def render(context):
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(probe).result(timeout=10), "LOCKED")
            return real(context)
        with patch("apps.quotes.pdf.generate_quote_pdf", side_effect=render):
            self.issue(r)
        self.assertEqual(len(self.files()), 1)

    async def test_async_preview_y_emision_materializados(self):
        from asgiref.sync import sync_to_async
        output = await aprevisualizar_comercial(self.quote.pk, [commercial_line()], actor=self.actor,
            company=COMPANY, conditions=["QA"])
        self.assertEqual(output["cliente"]["total"], D("178.47"))
        r = await sync_to_async(self.commercial_draft, thread_sensitive=True)()
        preview = await aprevisualizar_revision_comercial(r.pk, actor=self.actor, expected_version=1)
        issued = await aemitir_revision(r.pk, actor=self.actor, expected_version=1)
        self.assertEqual(preview["cliente"]["total"], issued.total)


class CommercialMigrationTests(TransactionTestCase):
    def test_forward_backward_aislado_conserva_v1_sin_defaults_comerciales(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        old = [("quotes", "0002_quoterevision_quotesnapshot")]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(old)
            state = executor.loader.project_state(old).apps
            user = get_user_model().objects.create_user(username="migration_p2_qa")
            quote = state.get_model("quotes", "Quote").objects.create(advisor_id=user.pk, folio="P2-HIST", client_name="QA")
            revision = state.get_model("quotes", "QuoteRevision").objects.create(quote_id=quote.pk, number=1,
                request_id=uuid4(), request_fingerprint="a" * 64, header={}, tax_rate=D(".16"),
                issue_date=timezone.localdate(), valid_days=15)
            snapshot = state.get_model("quotes", "QuoteSnapshot").objects.create(revision_id=revision.pk,
                position=1, name="Histórico QA", source_unit_price=D("1.004"), source_currency="MXN", quantity=D("1.50"))
            MigrationExecutor(connection).migrate(latest)
            r, s = QuoteRevision.objects.get(pk=revision.pk), QuoteSnapshot.objects.get(pk=snapshot.pk)
            self.assertEqual((r.schema_version, r.discount_percent, r.calculation_version), (1, None, None))
            self.assertEqual((s.source_unit_price, s.quantity, s.line_key), (D("1.004"), D("1.50"), None))
            MigrationExecutor(connection).migrate(old)
            self.assertTrue(state.get_model("quotes", "QuoteSnapshot").objects.filter(pk=snapshot.pk).exists())
        finally:
            MigrationExecutor(connection).migrate(latest)
