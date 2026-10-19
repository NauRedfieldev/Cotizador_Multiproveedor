"""Esquema comercial compatible: sin backfill de importes, margen, unidades o FX.

Solo DDL/permisos declarativos; no crea grupos, no asigna usuarios y no emite PDF.
"""

from decimal import Decimal

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q
from django.db.models.functions import Floor

REVISION_FIELDS = (
    "calculation_version", "state_fingerprint", "exchange_context_id", "discount_percent",
    "sale_subtotal", "discount_amount", "known_cost_total", "costs_complete", "gross_profit",
)
SNAPSHOT_FIELDS = (
    "line_key", "source_kind", "cost_tax_rate_included", "source_unit_label", "sale_unit_label",
    "conversion_factor", "unit_validated_by", "unit_validated_at", "pricing_mode", "margin_percent",
    "manual_unit_price", "manual_currency", "manual_tax_rate_included", "sale_amount", "cost_amount",
)


def decimal(digits, places):
    return models.DecimalField(max_digits=digits, decimal_places=places, null=True, blank=True)


class Migration(migrations.Migration):
    dependencies = [
        ("quotes", "0002_quoterevision_quotesnapshot"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(name="quoterevision", options={
            "ordering": ["quote_id", "number"], "permissions": (
                ("view_commercial_costs", "Consultar datos financieros internos autorizados"),
                ("change_commercial_margin", "Modificar margen comercial"),
                ("set_commercial_sale", "Capturar venta y conceptos manuales"),
                ("change_commercial_tax", "Modificar tasas de IVA comerciales"),
                ("change_commercial_fx", "Modificar contexto de tipo de cambio"),
                ("apply_commercial_discount", "Aplicar descuento global porcentual"),
                ("manage_commercial_permissions", "Gestionar permisos comerciales limitados"),
            ),
        }),
        *[migrations.AddField(model_name="quoterevision", name=name, field=field) for name, field in (
            ("calculation_version", models.PositiveSmallIntegerField(null=True, blank=True, editable=False)),
            ("state_fingerprint", models.CharField(max_length=64, null=True, blank=True, editable=False)),
            ("exchange_context_id", models.UUIDField(null=True, blank=True)),
            ("discount_percent", decimal(5, 2)), ("sale_subtotal", decimal(38, 2)),
            ("discount_amount", decimal(38, 2)), ("known_cost_total", decimal(38, 2)),
            ("costs_complete", models.BooleanField(null=True, blank=True)), ("gross_profit", decimal(38, 2)),
        )],
        *[migrations.AddField(model_name="quotesnapshot", name=name, field=field) for name, field in (
            ("line_key", models.UUIDField(null=True, blank=True)),
            ("source_kind", models.CharField(max_length=6, choices=[("API", "API"), ("MANUAL", "Manual")], null=True, blank=True)),
            ("cost_tax_rate_included", decimal(5, 4)),
            ("source_unit_label", models.CharField(max_length=80, null=True, blank=True)),
            ("sale_unit_label", models.CharField(max_length=80, null=True, blank=True)),
            ("conversion_factor", decimal(18, 6)),
            ("unit_validated_by", models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT,
                null=True, blank=True, related_name="validated_quote_units")),
            ("unit_validated_at", models.DateTimeField(null=True, blank=True)),
            ("pricing_mode", models.CharField(max_length=6, choices=[("MARGIN", "Margen"), ("MANUAL", "Manual")], null=True, blank=True)),
            ("margin_percent", decimal(4, 2)), ("manual_unit_price", decimal(18, 6)),
            ("manual_currency", models.CharField(max_length=3, choices=[("MXN", "MXN"), ("USD", "USD")], null=True, blank=True)),
            ("manual_tax_rate_included", decimal(5, 4)), ("sale_amount", decimal(38, 2)), ("cost_amount", decimal(38, 2)),
        )],
        migrations.AlterField(model_name="quotesnapshot", name="source_unit_price", field=decimal(18, 6)),
        migrations.AlterField(model_name="quotesnapshot", name="source_currency",
            field=models.CharField(max_length=3, choices=[("MXN", "MXN"), ("USD", "USD")], null=True, blank=True)),
        migrations.AddConstraint(model_name="quoterevision", constraint=models.CheckConstraint(condition=(
            (Q(schema_version=1) & Q(**{f"{f}__isnull": True for f in REVISION_FIELDS}))
            | (Q(schema_version=2, calculation_version=1, calculation_version__isnull=False, discount_percent__isnull=False,
                 discount_percent__gte=0, discount_percent__lte=100, state_fingerprint__isnull=False)
               & Q(state_fingerprint__regex=r"^[0-9a-f]{64}$"))
        ), name="quote_revision_commercial_version")),
        migrations.AddConstraint(model_name="quoterevision", constraint=models.CheckConstraint(condition=(
            Q(schema_version=1) | Q(exchange_rate__isnull=True, exchange_context_id__isnull=True)
            | Q(exchange_rate__isnull=False, exchange_context_id__isnull=False)
        ), name="quote_revision_commercial_fx")),
        migrations.AddConstraint(model_name="quoterevision", constraint=models.CheckConstraint(condition=(
            Q(schema_version=1)
            | Q(status="DRAFT", sale_subtotal__isnull=True, discount_amount__isnull=True,
                known_cost_total__isnull=True, costs_complete__isnull=True, gross_profit__isnull=True)
            | (Q(status="ISSUED", sale_subtotal__isnull=False, discount_amount__isnull=False,
                 known_cost_total__isnull=False, costs_complete__isnull=False,
                 sale_subtotal__gte=0, discount_amount__gte=0, known_cost_total__gte=0,
                 subtotal=F("sale_subtotal") - F("discount_amount"), discount_amount__lte=F("sale_subtotal"))
               & (Q(costs_complete=True, gross_profit__isnull=False, gross_profit=F("subtotal") - F("known_cost_total"))
                  | Q(costs_complete=False, gross_profit__isnull=True)))
        ), name="quote_revision_commercial_seal")),
        migrations.AddConstraint(model_name="quotesnapshot", constraint=models.UniqueConstraint(
            fields=["revision", "line_key"], name="quote_snapshot_line_unique")),
        migrations.AddConstraint(model_name="quotesnapshot", constraint=models.CheckConstraint(condition=(
            (Q(**{f"{f}__isnull": True for f in SNAPSHOT_FIELDS})
             & Q(source_unit_price__isnull=False, source_currency__isnull=False))
            | Q(source_kind__in=["API", "MANUAL"], source_kind__isnull=False, line_key__isnull=False, source_unit_label__isnull=False,
                sale_unit_label__isnull=False, conversion_factor__isnull=False, conversion_factor__gt=0,
                pricing_mode__isnull=False, unit_validated_by__isnull=False, unit_validated_at__isnull=False,
                amount__isnull=True)
        ), name="quote_snapshot_commercial_shape")),
        migrations.AddConstraint(model_name="quotesnapshot", constraint=models.CheckConstraint(condition=(
            Q(source_kind__isnull=True) | Q(source_unit_price__isnull=False, source_currency__isnull=False)
            | Q(source_kind="MANUAL", source_unit_price__isnull=True, source_currency__isnull=True,
                cost_tax_rate_included__isnull=True)
        ), name="quote_snapshot_cost_presence")),
        migrations.AddConstraint(model_name="quotesnapshot", constraint=models.CheckConstraint(condition=(
            Q(source_kind__isnull=True) | Q(source_kind="MANUAL", provider_code="", provider_name="", external_id="")
            | (Q(source_kind="API", source_unit_price__isnull=False) & ~Q(provider_code="") & ~Q(provider_name="") & ~Q(external_id=""))
        ), name="quote_snapshot_source_kind")),
        migrations.AddConstraint(model_name="quotesnapshot", constraint=models.CheckConstraint(condition=(
            Q(source_kind__isnull=True)
            | Q(pricing_mode="MARGIN", margin_percent__isnull=False, margin_percent__gte=0,
                margin_percent__lte=Decimal("99.99"), source_unit_price__isnull=False,
                manual_unit_price__isnull=True, manual_currency__isnull=True, manual_tax_rate_included__isnull=True)
            | Q(pricing_mode="MANUAL", margin_percent__isnull=True, manual_unit_price__isnull=False,
                manual_unit_price__gte=0, manual_currency__isnull=False, manual_currency__in=["MXN", "USD"])
        ), name="quote_snapshot_pricing_mode")),
        migrations.AddConstraint(model_name="quotesnapshot", constraint=models.CheckConstraint(condition=(
            Q(cost_tax_rate_included__isnull=True) | Q(cost_tax_rate_included__gte=0, cost_tax_rate_included__lte=1)
        ), name="quote_snapshot_cost_tax_range")),
        migrations.AddConstraint(model_name="quotesnapshot", constraint=models.CheckConstraint(condition=(
            Q(manual_tax_rate_included__isnull=True) | Q(manual_tax_rate_included__gte=0, manual_tax_rate_included__lte=1)
        ), name="quote_snapshot_sale_tax_range")),
        migrations.AddConstraint(model_name="quotesnapshot", constraint=models.CheckConstraint(condition=(
            Q(source_kind__isnull=True)
            | (Q(quantity=Floor(F("quantity"))) & (Q(sale_amount__isnull=True, cost_amount__isnull=True)
               | (Q(sale_amount__isnull=False, sale_amount__gte=0)
                  & (Q(source_unit_price__isnull=True, cost_amount__isnull=True)
                     | Q(source_unit_price__isnull=False, cost_amount__isnull=False, cost_amount__gte=0)))))
        ), name="quote_snapshot_commercial_amounts")),
        migrations.CreateModel(name="QuoteCommercialConfirmation", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("revision", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="commercial_confirmations", to="quotes.quoterevision")),
            ("edit_version", models.PositiveIntegerField()), ("state_fingerprint", models.CharField(max_length=64)),
            ("warning_code", models.CharField(max_length=32, choices=[(v, v) for v in (
                "MARGEN_CERO", "COSTO_DESCONOCIDO", "VENTA_BAJO_COSTO", "VENTA_CERO", "TOTAL_CERO")])),
            ("scope_ref", models.CharField(max_length=36, blank=True, default="")),
            ("confirmed_by", models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT,
                related_name="commercial_confirmations")),
            ("confirmed_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
        ], options={"constraints": [models.UniqueConstraint(fields=("revision", "edit_version", "state_fingerprint",
            "warning_code", "scope_ref", "confirmed_by"), name="quote_commercial_confirmation_unique")]}),
    ]
