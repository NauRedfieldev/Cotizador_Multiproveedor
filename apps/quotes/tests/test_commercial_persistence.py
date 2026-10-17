"""P2: persistencia/migración v2 en PostgreSQL de pruebas, sin APIs ni emisión v2."""

from dataclasses import replace
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.quotes.models import Quote, QuoteCommercialConfirmation, QuoteRevision, QuoteSnapshot
from apps.quotes.permissions import ADJUSTMENTS, configure_commercial_groups
from apps.quotes.snapshots import (
    PartidaComercialCapturada, RevisionConflict, _commercial_stored,
    _current_commercial_state,
    confirmar_advertencias_comerciales, crear_borrador_comercial, crear_revision_comercial,
    emitir_revision, guardar_borrador, guardar_borrador_comercial, validar_confirmaciones_comerciales,
)
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

    def commercial_edit(self, revision, parts=None, **kwargs):
        datos = dict(actor=self.actor, expected_version=revision.edit_version,
            company=revision.header["company"], conditions=revision.header["conditions"], client=revision.header["client"],
            tipo_cambio=None, exchange_context_id=None, tax_rate=revision.tax_rate,
            discount_percent=revision.discount_percent, issue_date=revision.issue_date, valid_days=revision.valid_days)
        datos.update(kwargs)
        parts = [PartidaComercialCapturada(**p) for p in _commercial_stored(revision)] if parts is None else parts
        return guardar_borrador_comercial(revision.pk, parts, **datos)

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

    def test_rutas_servicios_v1_y_emision_no_pueden_interpretar_v2(self):
        r = self.commercial_draft()
        with self.assertRaises(RevisionConflict):
            self.edit(r)
        with self.assertRaises(RevisionConflict):
            emitir_revision(r.pk, actor=self.actor, expected_version=1)
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
