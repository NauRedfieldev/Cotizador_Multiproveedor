"""P2: capacidades frescas, gestión limitada y confirmaciones vinculadas al estado."""

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from apps.quotes.models import QuoteCommercialConfirmation
from apps.quotes.admin import QuoteCommercialConfirmationAdmin
from apps.quotes.permissions import (
    ADMIN_GROUP, DISCOUNT, FX, MANAGE, MANUAL_SALE, MARGIN, TAX, VIEW_COSTS,
    configure_commercial_groups, require_commercial_permission, set_commercial_permission,
)
from apps.quotes.snapshots import (
    emitir_revision, obtener_documento, previsualizar_comercial,
    previsualizar_revision_comercial, validar_confirmaciones_comerciales,
)
from apps.quotes.tests.test_commercial import fx
from apps.quotes.tests.test_commercial_persistence import CommercialFixtures, commercial_line

D = Decimal


class CommercialPermissionsTests(CommercialFixtures, TestCase):
    def grant(self, capability, enabled=True, user=None):
        return set_commercial_permission((user or self.other).pk, capability, actor=self.actor, enabled=enabled)

    def other_quote(self):
        self.quote.advisor = self.other
        self.quote.save(update_fields=["advisor"])

    def test_admin_comercial_gestiona_sin_staff_ni_superusuario(self):
        self.assertFalse(self.actor.is_staff)
        self.assertFalse(self.actor.is_superuser)
        self.grant(MARGIN)
        self.assertEqual(require_commercial_permission(self.other, MARGIN).pk, self.other.pk)
        current = get_user_model().objects.get(pk=self.other.pk)
        self.assertFalse(current.is_staff)
        self.assertFalse(current.is_superuser)
        self.assertFalse(current.has_perm("auth.change_user"))

    def test_revocacion_no_confia_en_cache_del_objeto_actor(self):
        self.grant(MARGIN)
        stale = get_user_model().objects.get(pk=self.other.pk)
        self.assertTrue(stale.has_perm(f"quotes.{MARGIN}"))
        self.grant(MARGIN, False)
        self.assertTrue(stale.has_perm(f"quotes.{MARGIN}"))  # Django cache del objeto anterior.
        with self.assertRaises(PermissionDenied):
            require_commercial_permission(stale, MARGIN)

    def test_gestor_no_concede_lectura_interna_gestion_o_permisos_tecnicos(self):
        for capability in (VIEW_COSTS, MANAGE, "auth.change_user", "is_superuser", "change_user"):
            with self.subTest(capability=capability), self.assertRaises(ValidationError):
                self.grant(capability)
        self.assertFalse(self.other.groups.filter(name=ADMIN_GROUP).exists())

    def test_staff_no_es_administrador_comercial_ni_operador_manual(self):
        with self.assertRaises(PermissionDenied):
            set_commercial_permission(self.other.pk, MARGIN, actor=self.staff, enabled=True)
        with self.assertRaises(PermissionDenied):
            self.commercial_draft([commercial_line(sale="150")], actor=self.staff)

    def test_bootstrap_solo_tecnico_y_sin_asignar_miembros(self):
        with self.assertRaises(PermissionDenied):
            configure_commercial_groups(actor=self.actor)
        external = get_user_model().objects.create_user(username="externo_qa")
        configure_commercial_groups(actor=self.root)
        self.assertFalse(external.groups.exists())
        with self.assertRaises(PermissionDenied):
            set_commercial_permission(external.pk, MANUAL_SALE, actor=self.actor, enabled=True)

    def test_permiso_financiero_no_abre_cotizacion_ajena(self):
        self.grant(MANUAL_SALE)
        with self.assertRaises(PermissionDenied):
            self.commercial_draft([commercial_line(sale="100")], actor=self.other)

    def test_usuario_inactivo_y_no_interno_no_acceden(self):
        self.other.is_active = False
        self.other.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            require_commercial_permission(self.other)
        external = get_user_model().objects.create_user(username="externo_sin_costos_qa")
        self.quote.advisor = external
        self.quote.save(update_fields=["advisor"])
        with self.assertRaises(PermissionDenied):
            self.commercial_draft([commercial_line(kind="API")], actor=external)

    def test_ajustes_independientes_y_defaults_no_son_escalamiento(self):
        self.other_quote()
        r = self.commercial_draft([commercial_line(kind="API")], actor=self.other)
        self.assertEqual(r.discount_percent, D("0"))
        for kwargs, line in (
            ({}, commercial_line(kind="API", margin="40")),
            ({"discount_percent": D("10")}, commercial_line(kind="API")),
            ({"tax_rate": D(".08")}, commercial_line(kind="API")),
            ({}, commercial_line(sale="100")),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(PermissionDenied):
                self.commercial_draft([line], actor=self.other, **kwargs)

    def test_conservar_valor_previo_no_requiere_permiso_para_modificarlo(self):
        self.other_quote()
        self.grant(MARGIN)
        p = commercial_line(kind="API", margin="40")
        r = self.commercial_draft([p], actor=self.other)
        self.grant(MARGIN, False)
        preview = self.commercial_edit(r, [replace(p, quantity=D("2"))], actor=self.other, preview=True)
        self.assertFalse(preview["persistida"])
        self.assertEqual(preview["cliente"]["items"][0]["qty"], 2)
        with self.assertRaises(PermissionDenied):
            self.commercial_edit(r, [replace(p, margin_percent=D("45"))], actor=self.other, preview=True)
        edited = self.commercial_edit(r, [replace(p, quantity=D("2"))], actor=self.other)
        self.assertEqual(edited.edit_version, 2)
        with self.assertRaises(PermissionDenied):
            self.commercial_edit(edited, [replace(p, margin_percent=D("45"))], actor=self.other)

    def test_fx_requiere_permiso_y_no_se_reetiqueta_contexto_antiguo(self):
        self.other_quote()
        p, tipo, context = commercial_line(kind="API", source_currency="USD"), fx(), uuid4()
        with self.assertRaises(PermissionDenied):
            self.commercial_draft([p], actor=self.other, tipo_cambio=tipo, exchange_context_id=context)
        self.grant(FX)
        r = self.commercial_draft([p], actor=self.other, tipo_cambio=tipo, exchange_context_id=context)
        with self.assertRaises(ValidationError):
            self.commercial_edit(r, [p], actor=self.other, tipo_cambio=fx("18"), exchange_context_id=context)
        self.grant(FX, False)
        self.commercial_edit(r, [replace(p, quantity=D("2"))], actor=self.other,
                             tipo_cambio=tipo, exchange_context_id=context)

    def test_confirmacion_exige_facultad_correspondiente_y_revocacion_la_invalida(self):
        self.other_quote()
        self.grant(MARGIN)
        p = commercial_line(kind="API", margin="0")
        r = self.commercial_draft([p], actor=self.other)
        self.confirm(r, [("MARGEN_CERO", str(p.line_key))], actor=self.other)
        validar_confirmaciones_comerciales(r.pk, actor=self.other, expected_version=1)
        self.grant(MARGIN, False)
        with self.assertRaises(PermissionDenied):
            validar_confirmaciones_comerciales(r.pk, actor=self.other, expected_version=1)
        with self.assertRaises(PermissionDenied):
            self.confirm(r, [("MARGEN_CERO", str(p.line_key))], actor=self.other)

    def test_total_cero_por_descuento_no_se_confirma_solo_con_venta_manual(self):
        self.other_quote()
        self.grant(DISCOUNT)
        p = commercial_line(kind="API")
        r = self.commercial_draft([p], actor=self.other, discount_percent=D("100"))
        self.grant(DISCOUNT, False)
        self.grant(MANUAL_SALE)
        with self.assertRaises(PermissionDenied):
            self.confirm(r, [("TOTAL_CERO", "")], actor=self.other)

    def test_todos_los_avisos_deben_estar_confirmados(self):
        p = commercial_line(cost="0", sale="0")
        r = self.commercial_draft([p])
        self.confirm(r, [("TOTAL_CERO", "")])
        with self.assertRaises(PermissionDenied):
            validar_confirmaciones_comerciales(r.pk, actor=self.actor, expected_version=1)
        self.confirm(r, [("VENTA_CERO", str(p.line_key))])
        result = validar_confirmaciones_comerciales(r.pk, actor=self.actor, expected_version=1)
        self.assertEqual(result.total, D("0"))
        self.assertEqual(QuoteCommercialConfirmation.objects.count(), 2)

    def test_no_finge_revocar_permiso_heredado_de_grupo(self):
        with self.assertRaises(ValidationError):
            self.grant(MARGIN, False, user=self.actor)

    def test_admin_readonly_no_filtra_datos_a_staff_tecnico_sin_permiso_interno(self):
        technical = get_user_model().objects.create_user(username="staff_tecnico_qa", is_staff=True)
        technical.user_permissions.add(Permission.objects.get(content_type__app_label="quotes",
            codename="view_quotecommercialconfirmation"))
        model_admin = QuoteCommercialConfirmationAdmin(QuoteCommercialConfirmation, admin.site)
        self.assertFalse(model_admin.has_view_permission(SimpleNamespace(user=technical)))
        technical.groups.add(self.internal)
        technical = get_user_model().objects.get(pk=technical.pk)
        self.assertTrue(model_admin.has_view_permission(SimpleNamespace(user=technical)))

    def test_preview_y_emision_no_abren_quote_ajena_con_capacidades(self):
        p = commercial_line()
        r = self.commercial_draft([p])
        self.grant(MANUAL_SALE)
        for action in (
            lambda: previsualizar_comercial(self.quote.pk, [p], actor=self.other, company=r.header["company"], conditions=[]),
            lambda: previsualizar_revision_comercial(r.pk, actor=self.other, expected_version=1),
            lambda: emitir_revision(r.pk, actor=self.other, expected_version=1),
            lambda: obtener_documento(self.quote.folio, actor=self.other),
        ):
            with self.subTest(action=action), self.assertRaises(PermissionDenied):
                action()
        self.assertFalse(self.files())

    def test_staff_sin_lectura_financiera_no_previsualiza_emite_ni_descarga_v2(self):
        r = self.commercial_draft()
        self.staff.groups.remove(self.internal)
        for action in (
            lambda: previsualizar_revision_comercial(r.pk, actor=self.staff, expected_version=1),
            lambda: emitir_revision(r.pk, actor=self.staff, expected_version=1),
            lambda: obtener_documento(self.quote.folio, actor=self.staff),
        ):
            with self.subTest(action=action), self.assertRaises(PermissionDenied):
                action()

    def test_preview_sin_guardar_aplica_facultades_de_creacion(self):
        self.other_quote()
        p = commercial_line(kind="API", margin="40")
        with self.assertRaises(PermissionDenied):
            previsualizar_comercial(self.quote.pk, [p], actor=self.other,
                company={"name": "QA"}, conditions=[])
        self.grant(MARGIN)
        output = previsualizar_comercial(self.quote.pk, [p], actor=self.other,
            company={"name": "QA"}, conditions=[])
        self.assertEqual(output["cliente"]["total"], D("193.34"))

    def test_inactivo_en_bd_no_pasa_emision_con_actor_cacheado_activo(self):
        r = self.commercial_draft()
        current = get_user_model().objects.get(pk=self.actor.pk)
        current.is_active = False
        current.save(update_fields=["is_active"])
        self.assertTrue(self.actor.is_active)
        with self.assertRaises(PermissionDenied):
            emitir_revision(r.pk, actor=self.actor, expected_version=1)
