"""Cotizaciones legacy y revisiones/snapshots M8 con sellado inmutable.

DER completo y diccionario de datos en docs/DER.md.
"""

import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Floor
from django.utils import timezone

from .snapshot_values import decimal_limit
from .storage import QuotePDFStorage, quote_pdf_path
from .taxes import TASA_IVA_INICIAL, validar_tasa

TWO_PLACES = Decimal("0.01")
DEFAULT_TAX_RATE = TASA_IVA_INICIAL  # M10: la tasa inicial vive en taxes.py

REVISION_COMMERCIAL_FIELDS = (
    "calculation_version", "state_fingerprint", "exchange_context_id", "discount_percent",
    "sale_subtotal", "discount_amount", "known_cost_total", "costs_complete", "gross_profit",
)
SNAPSHOT_COMMERCIAL_FIELDS = (
    "line_key", "source_kind", "cost_tax_rate_included", "source_unit_label", "sale_unit_label",
    "conversion_factor", "unit_validated_by_id", "unit_validated_at", "pricing_mode", "margin_percent",
    "manual_unit_price", "manual_currency", "manual_tax_rate_included", "sale_amount", "cost_amount",
)
COMMERCIAL_PERMISSIONS = (
    ("view_commercial_costs", "Consultar datos financieros internos autorizados"),
    ("change_commercial_margin", "Modificar margen comercial"),
    ("set_commercial_sale", "Capturar venta y conceptos manuales"),
    ("change_commercial_tax", "Modificar tasas de IVA comerciales"),
    ("change_commercial_fx", "Modificar contexto de tipo de cambio"),
    ("apply_commercial_discount", "Aplicar descuento global porcentual"),
    ("manage_commercial_permissions", "Gestionar permisos comerciales limitados"),
)


def _money(value):
    """Redondea a 2 decimales con regla comercial (ROUND_HALF_UP)."""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


rfc_validator = RegexValidator(
    regex=r"^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$",
    message="RFC inválido: formato oficial (12-13 caracteres, mayúsculas, sin guiones).",
)


class Quote(models.Model):
    """Cabecera de la cotización (propuesta comercial).

    `subtotal`, `tax` y `total` son propiedades calculadas desde las
    partidas; no se persisten para evitar datos desnormalizados.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Borrador"
        ISSUED = "ISSUED", "Emitida"
        SENT = "SENT", "Enviada"
        ACCEPTED = "ACCEPTED", "Aceptada"

    folio = models.CharField("folio", max_length=20, unique=True, editable=False)
    client_name = models.CharField("razón social del cliente", max_length=255)
    client_rfc = models.CharField(
        "RFC del cliente", max_length=13, blank=True, validators=[rfc_validator]
    )
    client_email = models.EmailField("correo del cliente", blank=True)
    advisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="quotes",
        verbose_name="asesor técnico",
    )
    issue_date = models.DateField("fecha de emisión", default=timezone.localdate)
    valid_days = models.PositiveSmallIntegerField("días de vigencia", default=15)
    status = models.CharField(
        "estatus", max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    tax_rate = models.DecimalField(
        "tasa de IVA", max_digits=5, decimal_places=4, default=DEFAULT_TAX_RATE
    )
    notes = models.TextField("notas / condiciones comerciales", blank=True)
    created_at = models.DateTimeField("creada", auto_now_add=True)
    updated_at = models.DateTimeField("actualizada", auto_now=True)

    class Meta:
        verbose_name = "cotización"
        verbose_name_plural = "cotizaciones"
        ordering = ["-issue_date", "-id"]

    def __str__(self):
        return f"{self.folio} — {self.client_name}"

    # ------------------------------------------------------------------ #
    # Folio
    # ------------------------------------------------------------------ #

    def save(self, *args, **kwargs):
        if not self.folio:
            self.folio = self._generate_folio()
        super().save(*args, **kwargs)

    def _generate_folio(self):
        """Genera folio correlativo PREFIJO-YYYY-XXXX (el consecutivo reinicia cada año y es propio de cada prefijo).

        5A: `folio_prefix` (atributo, no columna) lo pone el alta según la empresa; por defecto COT (CCONOR).
        La unicidad la garantiza el constraint `unique` en BD; ante una
        colisión por concurrencia basta reintentar el guardado.
        """
        base = getattr(self, "folio_prefix", "") or "COT"
        if not re.fullmatch(r"[A-Z]{2,6}", base):
            raise ValidationError("Prefijo de folio inválido: 2 a 6 letras mayúsculas.")
        prefix = f"{base}-{timezone.localdate().year}-"
        last_folio = (
            Quote.objects.filter(folio__startswith=prefix)
            .order_by("-folio")
            .values_list("folio", flat=True)
            .first()
        )
        number = int(last_folio.rsplit("-", 1)[1]) + 1 if last_folio else 1
        return f"{prefix}{number:04d}"

    # ------------------------------------------------------------------ #
    # Fechas
    # ------------------------------------------------------------------ #

    @property
    def expiration_date(self):
        """Fecha límite de validez = fecha de emisión + días de vigencia."""
        if not self.issue_date:
            return None
        return self.issue_date + timedelta(days=self.valid_days)

    @property
    def is_expired(self):
        return bool(self.expiration_date) and timezone.localdate() > self.expiration_date

    # ------------------------------------------------------------------ #
    # Importes (calculados, no persistidos)
    # ------------------------------------------------------------------ #

    @property
    def subtotal(self):
        """Suma de los importes de las partidas, antes de IVA."""
        if self.pk is None:
            return Decimal("0.00")
        result = self.items.aggregate(
            subtotal=Sum(F("quantity") * F("unit_price"))
        )["subtotal"]
        return _money(result or Decimal("0.00"))

    @property
    def tax(self):
        """IVA = subtotal × tasa de la cotización."""
        return _money(self.subtotal * self.tax_rate)

    @property
    def total(self):
        return self.subtotal + self.tax


class QuoteItem(models.Model):
    """Partida o línea de la cotización.

    Puede ligarse a un Product del catálogo o ser un concepto personalizado
    (product=None). `description` y `unit_price` son snapshots: al guardar,
    si vienen vacíos se copian del producto ligado para congelar el precio
    y la descripción al momento de cotizar.
    """

    quote = models.ForeignKey(
        Quote, on_delete=models.CASCADE, related_name="items", verbose_name="cotización"
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quote_items",
        verbose_name="producto",
    )
    description = models.TextField("descripción de la partida", blank=True)
    quantity = models.DecimalField(
        "cantidad",
        max_digits=10,
        decimal_places=2,
        default=Decimal("1.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    unit_price = models.DecimalField(
        "precio unitario",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    class Meta:
        verbose_name = "partida"
        verbose_name_plural = "partidas"
        ordering = ["id"]

    def __str__(self):
        return f"{self.quote.folio} · {self.quantity} × {self.unit_price}"

    def save(self, *args, **kwargs):
        # Snapshot de catálogo: congela descripción y precio si no se dieron.
        if self.product_id:
            if not self.description:
                self.description = self.product.description or self.product.name
            if not self.unit_price:
                self.unit_price = self.product.unit_price
        super().save(*args, **kwargs)

    @property
    def amount(self):
        """Importe de la partida = cantidad × precio unitario."""
        return _money((self.quantity or Decimal("0")) * (self.unit_price or Decimal("0")))


class RevisionQuerySet(models.QuerySet):
    """Las escrituras masivas omiten validación y bloqueos: no se permiten en M8."""

    def update(self, **kwargs):
        raise ValidationError("Usa los servicios de revisiones; update no está permitido.")

    def delete(self):
        raise ValidationError("No se permite borrar revisiones/snapshots en bloque.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("bulk_create omite las garantías de M8.")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("bulk_update omite las garantías de M8.")


class QuoteRevision(models.Model):
    """Cabecera por versión. Solo los servicios pueden sellarla y archivar su PDF."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Borrador"
        ISSUED = "ISSUED", "Emitida"

    quote = models.ForeignKey(Quote, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    based_on = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="derived_revisions")
    request_id = models.UUIDField(default=uuid4, editable=False)
    request_fingerprint = models.CharField(max_length=64, editable=False)
    edit_version = models.PositiveIntegerField(default=1, editable=False)
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.DRAFT)
    schema_version = models.PositiveSmallIntegerField(default=1, editable=False)
    calculation_version = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)
    state_fingerprint = models.CharField(max_length=64, null=True, blank=True, editable=False)
    exchange_context_id = models.UUIDField(null=True, blank=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    sale_subtotal = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)
    known_cost_total = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)
    costs_complete = models.BooleanField(null=True, blank=True)
    gross_profit = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)
    header = models.JSONField()  # client/company/conditions; solo texto validado por el servicio.
    currency = models.CharField(max_length=3, default="MXN", editable=False)
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=8, null=True, blank=True)
    exchange_rate_source = models.CharField(max_length=120, blank=True)
    exchange_rate_date = models.DateTimeField(null=True, blank=True)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=4,
                                  validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))])
    issue_date = models.DateField()
    valid_days = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    subtotal = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)
    tax = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)
    pdf = models.FileField(storage=QuotePDFStorage(), upload_to=quote_pdf_path, max_length=200, blank=True)
    pdf_sha256 = models.CharField(max_length=64, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RevisionQuerySet.as_manager()

    class Meta:
        ordering = ["quote_id", "number"]
        permissions = COMMERCIAL_PERMISSIONS
        constraints = [
            models.UniqueConstraint(fields=["quote", "number"], name="quote_revision_number_unique"),
            models.UniqueConstraint(fields=["quote", "request_id"], name="quote_revision_request_unique"),
            models.CheckConstraint(condition=Q(number__gte=1), name="quote_revision_number_positive"),
            models.CheckConstraint(condition=Q(currency="MXN"), name="quote_revision_currency_mxn"),
            models.CheckConstraint(condition=Q(tax_rate__gte=0, tax_rate__lte=1), name="quote_revision_tax_range"),
            models.CheckConstraint(condition=(
                Q(status="DRAFT", issued_at__isnull=True, pdf="", pdf_sha256="", subtotal__isnull=True,
                  tax__isnull=True, total__isnull=True)
                | (Q(status="ISSUED", issued_at__isnull=False, subtotal__isnull=False, tax__isnull=False,
                     total__isnull=False, subtotal__gte=0, tax__gte=0, total__gte=0)
                   & ~Q(pdf="") & ~Q(pdf_sha256=""))
            ), name="quote_revision_seal_consistent"),
            models.CheckConstraint(condition=(
                (Q(schema_version=1) & Q(**{f"{f}__isnull": True for f in REVISION_COMMERCIAL_FIELDS}))
                | (Q(schema_version=2, calculation_version=1, calculation_version__isnull=False, discount_percent__isnull=False,
                     discount_percent__gte=0, discount_percent__lte=100, state_fingerprint__isnull=False)
                   & Q(state_fingerprint__regex=r"^[0-9a-f]{64}$"))
            ), name="quote_revision_commercial_version"),
            models.CheckConstraint(condition=(Q(schema_version=1)
                | Q(exchange_rate__isnull=True, exchange_context_id__isnull=True)
                | Q(exchange_rate__isnull=False, exchange_context_id__isnull=False)
            ), name="quote_revision_commercial_fx"),
            models.CheckConstraint(condition=(Q(schema_version=1)
                | Q(status="DRAFT", sale_subtotal__isnull=True, discount_amount__isnull=True,
                    known_cost_total__isnull=True, costs_complete__isnull=True, gross_profit__isnull=True)
                | (Q(status="ISSUED", sale_subtotal__isnull=False, discount_amount__isnull=False,
                     known_cost_total__isnull=False, costs_complete__isnull=False,
                     sale_subtotal__gte=0, discount_amount__gte=0, known_cost_total__gte=0,
                     subtotal=F("sale_subtotal") - F("discount_amount"),
                     discount_amount__lte=F("sale_subtotal"))
                   & (Q(costs_complete=True, gross_profit__isnull=False,
                        gross_profit=F("subtotal") - F("known_cost_total"))
                      | Q(costs_complete=False, gross_profit__isnull=True)))
            ), name="quote_revision_commercial_seal"),
        ]

    def clean(self):
        self.tax_rate = validar_tasa(self.tax_rate)
        if self.currency != "MXN":
            raise ValidationError("M8 emite en MXN.")
        if self.exchange_rate is not None:
            self.exchange_rate = decimal_limit(self.exchange_rate, 18, 8, minimum=Decimal("0.00000001"))
            if not self.exchange_rate_source.strip() or self.exchange_rate_date is None or self.exchange_rate_date.utcoffset() is None:
                raise ValidationError("El tipo de cambio requiere origen y fecha con zona horaria.")
        elif self.exchange_rate_source or self.exchange_rate_date is not None:
            raise ValidationError("No guardar procedencia de un tipo inexistente.")
        if self.based_on_id and self.based_on.quote_id != self.quote_id:
            raise ValidationError("La revisión de origen pertenece a otra cotización.")
        if self.schema_version == 1:
            if any(getattr(self, f) is not None for f in REVISION_COMMERCIAL_FIELDS):
                raise ValidationError("Una revisión v1 no recibe campos comerciales v2.")
        elif self.schema_version == 2:
            if self.calculation_version != 1 or self.discount_percent is None:
                raise ValidationError("Versión de cálculo/descuento comercial requeridos.")
            if not isinstance(self.state_fingerprint, str) or len(self.state_fingerprint) != 64 or any(
                    c not in "0123456789abcdef" for c in self.state_fingerprint):
                raise ValidationError("Huella de estado comercial inválida.")
            if (self.exchange_rate is None) != (self.exchange_context_id is None):
                raise ValidationError("El FX comercial requiere identidad de contexto coherente.")
        else:
            raise ValidationError("Versión de esquema no soportada.")

    def clean_fields(self, exclude=None):
        self.tax_rate = validar_tasa(self.tax_rate)
        if self.exchange_rate is not None:
            self.exchange_rate = decimal_limit(self.exchange_rate, 18, 8, minimum=Decimal("0.00000001"))
        if self.discount_percent is not None:
            self.discount_percent = decimal_limit(self.discount_percent, 5, 2, maximum=Decimal("100"))
        for field in ("subtotal", "tax", "total", "sale_subtotal", "discount_amount", "known_cost_total"):
            if getattr(self, field) is not None:
                setattr(self, field, decimal_limit(getattr(self, field), 38, 2))
        if self.gross_profit is not None:
            if not isinstance(self.gross_profit, Decimal):
                raise ValidationError("La utilidad debe ser Decimal.")
            decimal_limit(self.gross_profit.copy_abs(), 38, 2)
        super().clean_fields(exclude=exclude)

    def save(self, *args, _seal=False, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.select_for_update().get(pk=self.pk)
                if previous.status == self.Status.ISSUED:
                    raise ValidationError("La revisión emitida es inmutable; crea otra revisión.")
                if any(getattr(previous, f) != getattr(self, f) for f in (
                    "quote_id", "number", "based_on_id", "request_id", "request_fingerprint", "schema_version",
                    "calculation_version",
                )):
                    raise ValidationError("No se cambia la identidad de una revisión.")
            if self.status != self.Status.DRAFT and not _seal:
                raise ValidationError("Solo emitir_revision puede sellar el documento.")
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("No se borran revisiones; conserva el historial.")


class QuoteSnapshot(models.Model):
    """Partida capturada, independiente del Product vigente y sin payload raw."""

    revision = models.ForeignKey(QuoteRevision, on_delete=models.PROTECT, related_name="snapshots")
    position = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    code = models.TextField(blank=True)
    name = models.TextField()
    description = models.TextField(blank=True)
    provider_code = models.CharField(max_length=80, blank=True)
    provider_name = models.CharField(max_length=255, blank=True)
    external_id = models.TextField(blank=True)
    brand = models.TextField(blank=True)
    model = models.TextField(blank=True)
    mpn = models.TextField(blank=True)
    source_unit_price = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    source_currency = models.CharField(max_length=3, choices=[("MXN", "MXN"), ("USD", "USD")], null=True, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.PositiveBigIntegerField(null=True, blank=True)
    es_mejor_precio = models.BooleanField(default=False)
    amount = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(default=timezone.now)
    line_key = models.UUIDField(null=True, blank=True)
    source_kind = models.CharField(max_length=6, choices=[("API", "API"), ("MANUAL", "Manual")], null=True, blank=True)
    cost_tax_rate_included = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    source_unit_label = models.CharField(max_length=80, null=True, blank=True)
    sale_unit_label = models.CharField(max_length=80, null=True, blank=True)
    conversion_factor = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    unit_validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                         null=True, blank=True, related_name="validated_quote_units")
    unit_validated_at = models.DateTimeField(null=True, blank=True)
    pricing_mode = models.CharField(max_length=6, choices=[("MARGIN", "Margen"), ("MANUAL", "Manual")], null=True, blank=True)
    margin_percent = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    manual_unit_price = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    manual_currency = models.CharField(max_length=3, choices=[("MXN", "MXN"), ("USD", "USD")], null=True, blank=True)
    manual_tax_rate_included = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    sale_amount = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)
    cost_amount = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)

    objects = RevisionQuerySet.as_manager()

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["revision", "position"], name="quote_snapshot_position_unique"),
            models.CheckConstraint(condition=Q(source_unit_price__gte=0, quantity__gt=0, position__gte=1),
                                   name="quote_snapshot_values_positive"),
            models.CheckConstraint(condition=Q(source_currency__in=["MXN", "USD"]), name="quote_snapshot_currency_supported"),
            models.UniqueConstraint(fields=["revision", "line_key"], name="quote_snapshot_line_unique"),
            models.CheckConstraint(condition=(
                (Q(**{f"{f.removesuffix('_id')}__isnull": True for f in SNAPSHOT_COMMERCIAL_FIELDS})
                 & Q(source_unit_price__isnull=False, source_currency__isnull=False))
                | Q(source_kind__in=["API", "MANUAL"], source_kind__isnull=False, line_key__isnull=False,
                    source_unit_label__isnull=False, sale_unit_label__isnull=False,
                    conversion_factor__isnull=False, conversion_factor__gt=0,
                    pricing_mode__isnull=False,
                    unit_validated_by__isnull=False, unit_validated_at__isnull=False,
                    amount__isnull=True)
            ), name="quote_snapshot_commercial_shape"),
            models.CheckConstraint(condition=(Q(source_kind__isnull=True)
                | Q(source_unit_price__isnull=False, source_currency__isnull=False)
                | Q(source_kind="MANUAL", source_unit_price__isnull=True,
                    source_currency__isnull=True, cost_tax_rate_included__isnull=True)
            ), name="quote_snapshot_cost_presence"),
            models.CheckConstraint(condition=(Q(source_kind__isnull=True)
                | Q(source_kind="MANUAL", provider_code="", provider_name="", external_id="")
                | (Q(source_kind="API", source_unit_price__isnull=False) & ~Q(provider_code="")
                   & ~Q(provider_name="") & ~Q(external_id=""))
            ), name="quote_snapshot_source_kind"),
            models.CheckConstraint(condition=(Q(source_kind__isnull=True)
                | Q(pricing_mode="MARGIN", margin_percent__isnull=False, margin_percent__gte=0,
                    margin_percent__lte=Decimal("99.99"), source_unit_price__isnull=False,
                    manual_unit_price__isnull=True, manual_currency__isnull=True, manual_tax_rate_included__isnull=True)
                | Q(pricing_mode="MANUAL", margin_percent__isnull=True, manual_unit_price__isnull=False,
                    manual_unit_price__gte=0, manual_currency__isnull=False, manual_currency__in=["MXN", "USD"])
            ), name="quote_snapshot_pricing_mode"),
            models.CheckConstraint(condition=(Q(cost_tax_rate_included__isnull=True)
                | Q(cost_tax_rate_included__gte=0, cost_tax_rate_included__lte=1)
            ), name="quote_snapshot_cost_tax_range"),
            models.CheckConstraint(condition=(Q(manual_tax_rate_included__isnull=True)
                | Q(manual_tax_rate_included__gte=0, manual_tax_rate_included__lte=1)
            ), name="quote_snapshot_sale_tax_range"),
            models.CheckConstraint(condition=(Q(source_kind__isnull=True)
                | (Q(quantity=Floor(F("quantity")))
                   & (Q(sale_amount__isnull=True, cost_amount__isnull=True)
                      | (Q(sale_amount__isnull=False, sale_amount__gte=0)
                         & (Q(source_unit_price__isnull=True, cost_amount__isnull=True)
                            | Q(source_unit_price__isnull=False, cost_amount__isnull=False, cost_amount__gte=0)))))
            ), name="quote_snapshot_commercial_amounts"),
        ]

    def clean(self):
        if self.source_unit_price is not None:
            self.source_unit_price = decimal_limit(self.source_unit_price, 18, 6)
        self.quantity = decimal_limit(self.quantity, 10, 2, minimum=Decimal("0.01"))
        if self.stock is not None and (type(self.stock) is not int or self.stock < 0):
            raise ValidationError("Stock debe ser entero no negativo o None.")
        if bool(self.provider_code) != bool(self.external_id) or (self.provider_code and not self.provider_name):
            raise ValidationError("La oferta requiere proveedor, nombre e ID externo coherentes.")
        if self.revision.schema_version == 1:
            if self.source_unit_price is None or self.source_currency is None or any(
                    getattr(self, f) is not None for f in SNAPSHOT_COMMERCIAL_FIELDS):
                raise ValidationError("Partida v1 requiere original y no admite campos v2.")
        elif self.revision.schema_version == 2:
            self.commercial_input()  # Valida la combinación sin redondear entradas.
            if self.amount is not None or self.line_key is None or self.source_kind not in ("API", "MANUAL"):
                raise ValidationError("Identidad/origen comercial inválidos.")
            if not self.source_unit_label or not self.source_unit_label.strip() or not self.sale_unit_label or not self.sale_unit_label.strip():
                raise ValidationError("Las unidades de referencia y venta son explícitas.")
            if self.unit_validated_by_id is None or self.unit_validated_at is None or self.unit_validated_at.utcoffset() is None:
                raise ValidationError("Validación de unidades sin actor/fecha válida.")
            if self.source_kind == "API" and not self.provider_code:
                raise ValidationError("Una captura API requiere proveedor.")
            if self.source_kind == "MANUAL" and (self.provider_code or self.provider_name or self.external_id):
                raise ValidationError("No se atribuye una captura manual a una API.")
            if self.source_unit_price is None and (self.source_currency is not None or self.cost_tax_rate_included is not None):
                raise ValidationError("No se inventa moneda/tasa para un costo desconocido.")
        else:
            raise ValidationError("Versión de partida no soportada.")

    def commercial_input(self):
        from .commercial import PartidaComercial, PrecioCapturado

        if self.quantity is None or self.quantity != self.quantity.to_integral_value():
            raise ValidationError("La cantidad comercial debe ser entera.")
        if self.pricing_mode not in ("MARGIN", "MANUAL"):
            raise ValidationError("Modalidad de venta requerida.")
        return PartidaComercial(
            str(self.line_key), int(self.quantity),
            None if self.source_unit_price is None else PrecioCapturado(
                self.source_unit_price, self.source_currency, self.cost_tax_rate_included),
            margen=self.margin_percent,
            venta_manual=None if self.manual_unit_price is None else PrecioCapturado(
                self.manual_unit_price, self.manual_currency, self.manual_tax_rate_included),
            factor_conversion=self.conversion_factor,
            existencias=self.stock, aplica_existencias=self.source_kind == "API",
        )

    def clean_fields(self, exclude=None):
        if self.source_unit_price is not None:
            self.source_unit_price = decimal_limit(self.source_unit_price, 18, 6)
        self.quantity = decimal_limit(self.quantity, 10, 2, minimum=Decimal("0.01"))
        if self.stock is not None and (type(self.stock) is not int or not 0 <= self.stock <= 9223372036854775807):
            raise ValidationError("Stock fuera de rango o no entero.")
        if type(self.es_mejor_precio) is not bool:
            raise ValidationError("es_mejor_precio debe ser bool.")
        for field, digits, places, minimum, maximum in (
            ("amount", 38, 2, Decimal("0"), None), ("sale_amount", 38, 2, Decimal("0"), None),
            ("cost_amount", 38, 2, Decimal("0"), None), ("manual_unit_price", 18, 6, Decimal("0"), None),
            ("margin_percent", 4, 2, Decimal("0"), Decimal("99.99")),
            ("conversion_factor", 18, 6, Decimal("0.000001"), None),
            ("cost_tax_rate_included", 5, 4, Decimal("0"), Decimal("1")),
            ("manual_tax_rate_included", 5, 4, Decimal("0"), Decimal("1")),
        ):
            if getattr(self, field) is not None:
                setattr(self, field, decimal_limit(getattr(self, field), digits, places, minimum=minimum, maximum=maximum))
        super().clean_fields(exclude=exclude)

    def save(self, *args, _seal=False, **kwargs):
        with transaction.atomic():
            parent = QuoteRevision.objects.select_for_update().get(pk=self.revision_id)
            self._state.fields_cache["revision"] = parent
            if parent.status == QuoteRevision.Status.ISSUED:
                raise ValidationError("No se modifica una partida emitida.")
            if self.pk and type(self).objects.get(pk=self.pk).revision_id != self.revision_id:
                raise ValidationError("No se mueve una partida a otra revisión.")
            if (self.source_currency == "USD" or self.manual_currency == "USD") and parent.exchange_rate is None:
                raise ValidationError("USD requiere tipo de cambio explícito.")
            if any(getattr(self, f) is not None for f in ("amount", "sale_amount", "cost_amount")) and not _seal:
                raise ValidationError("Solo la emisión guarda el importe congelado.")
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk and type(self).objects.get(pk=self.pk).revision_id != self.revision_id:
                raise ValidationError("No se cambia la revisión al borrar una partida.")
            parent = QuoteRevision.objects.select_for_update().get(pk=self.revision_id)
            if parent.status == QuoteRevision.Status.ISSUED:
                raise ValidationError("No se borra una partida emitida.")
            return super().delete(*args, **kwargs)


class QuoteCommercialConfirmation(models.Model):
    """Constancia inmutable; solo el servicio verifica acceso/huella/permisos."""

    revision = models.ForeignKey(QuoteRevision, on_delete=models.PROTECT, related_name="commercial_confirmations")
    edit_version = models.PositiveIntegerField()
    state_fingerprint = models.CharField(max_length=64)
    warning_code = models.CharField(max_length=32, choices=[(v, v) for v in (
        "MARGEN_CERO", "COSTO_DESCONOCIDO", "VENTA_BAJO_COSTO", "VENTA_CERO", "TOTAL_CERO",
        "STOCK_DESCONOCIDO")])  # Avisos de stock informativos no se confirman ni se guardan.
    scope_ref = models.CharField(max_length=36, blank=True, default="")
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                     related_name="commercial_confirmations")
    confirmed_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = RevisionQuerySet.as_manager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=("revision", "edit_version", "state_fingerprint",
            "warning_code", "scope_ref", "confirmed_by"), name="quote_commercial_confirmation_unique")]

    def clean(self):
        if self.revision.schema_version != 2 or self.revision.status != QuoteRevision.Status.DRAFT:
            raise ValidationError("Solo se confirma un borrador comercial.")
        if self.edit_version != self.revision.edit_version or self.state_fingerprint != self.revision.state_fingerprint:
            raise ValidationError("La confirmación no corresponde al estado actual.")
        if self.confirmed_at is None or self.confirmed_at.utcoffset() is None:
            raise ValidationError("La constancia requiere una fecha con zona horaria.")
        if self.scope_ref:
            try:
                key = UUID(self.scope_ref)
            except (ValueError, TypeError):
                raise ValidationError("Referencia de advertencia inválida.") from None
            if str(key) != self.scope_ref or not self.revision.snapshots.filter(line_key=key).exists():
                raise ValidationError("La partida no pertenece a la revisión.")

    def save(self, *args, _confirm=False, **kwargs):
        if self.pk or not _confirm:
            raise ValidationError("Usa el servicio de confirmación; la constancia es inmutable.")
        with transaction.atomic():
            Quote.objects.select_for_update().get(pk=self.revision.quote_id)
            parent = QuoteRevision.objects.select_for_update().get(pk=self.revision_id)
            self._state.fields_cache["revision"] = parent
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("No se borran constancias de confirmación.")


class QuoteSelectionContext(models.Model):
    """Evidencia técnica normalizada e inmutable; no contiene payload raw."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                              related_name="quote_selection_contexts")
    kind = models.CharField(max_length=6, choices=[("SEARCH", "Búsqueda"), ("FX", "Tipo de cambio")])
    payload = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    objects = RevisionQuerySet.as_manager()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(kind__in=["SEARCH", "FX"]), name="quote_context_kind")]

    def clean(self):
        if not isinstance(self.payload, dict):
            raise ValidationError("Contexto normalizado requerido.")
        if self.kind == "FX":
            if set(self.payload) != {"value", "source", "date"}:
                raise ValidationError("Campos FX no admitidos.")
            try:
                if not all(isinstance(self.payload[k], str) for k in ("value", "source", "date")) or len(self.payload["value"]) > 80:
                    raise ValueError
                decimal_limit(Decimal(self.payload["value"]), 18, 8, minimum=Decimal(".00000001"))
                observed = datetime.fromisoformat(self.payload["date"])
                if observed.utcoffset() is None or not self.payload["source"].strip() or len(self.payload["source"]) > 120:
                    raise ValueError
            except (ValueError, TypeError, ArithmeticError, AttributeError):
                raise ValidationError("Evidencia FX inválida.") from None
        elif self.kind == "SEARCH":
            if set(self.payload) != {"offers", "fx_context_id"} or not isinstance(self.payload["offers"], dict) or not 1 <= len(self.payload["offers"]) <= 500:
                raise ValidationError("Captura de ofertas inválida.")
            allowed = {"name", "price", "currency", "provider_code", "provider_name", "external_id",
                       "brand", "model", "mpn", "stock", "es_mejor_precio", "vat"}
            try:
                ref = self.payload["fx_context_id"]
                if ref is not None and str(UUID(ref)) != ref:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise ValidationError("Referencia FX normalizada inválida.") from None
            for key, offer in self.payload["offers"].items():
                try:
                    if str(UUID(key)) != key or not isinstance(offer, dict) or set(offer) != allowed:
                        raise ValueError
                    for field in ("name", "provider_code", "provider_name", "external_id", "brand", "model", "mpn"):
                        value = offer[field]
                        limit = 80 if field == "provider_code" else 255 if field == "provider_name" else 6000
                        if not isinstance(value, str) or len(value) > limit or (field in ("name", "provider_code", "provider_name", "external_id") and not value.strip()):
                            raise ValueError
                    if not isinstance(offer["price"], str) or len(offer["price"]) > 80:
                        raise ValueError
                    decimal_limit(Decimal(offer["price"]), 18, 6)
                    if offer["currency"] not in ("MXN", "USD") or type(offer["es_mejor_precio"]) is not bool:
                        raise ValueError
                    if offer["stock"] is not None and (type(offer["stock"]) is not int or not 0 <= offer["stock"] <= 9223372036854775807):
                        raise ValueError
                    vat = offer["vat"]
                    if not isinstance(vat, dict):
                        raise ValueError
                    if vat.get("vat_basis") in ("NET", "UNKNOWN"):
                        if set(vat) != {"vat_basis"}:
                            raise ValueError
                    elif vat.get("vat_basis") == "INCLUDED" and set(vat) == {"vat_basis", "vat_rate"} and isinstance(vat["vat_rate"], str):
                        decimal_limit(Decimal(vat["vat_rate"]), 5, 4, maximum=Decimal("1"))
                    else:
                        raise ValueError
                except (ValueError, TypeError, AttributeError, ArithmeticError):
                    raise ValidationError("Oferta normalizada inválida; no guardar raw.") from None
        else:
            raise ValidationError("Tipo de contexto inválido.")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("La captura es inmutable; crea otra referencia.")
        self.full_clean()
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("No borrar evidencia de selección desde el modelo.")


class QuoteAPIRequest(models.Model):
    """Recibo de intención HTTP; resultado y recibo se confirman juntos."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="quote_api_requests")
    request_id = models.UUIDField()
    operation = models.CharField(max_length=8, choices=[("CREATE", "Alta"), ("CLONE", "Revisión"), ("FX", "FX")])
    fingerprint = models.CharField(max_length=64)
    revision = models.ForeignKey(QuoteRevision, null=True, blank=True, on_delete=models.PROTECT, related_name="api_requests")
    context = models.ForeignKey(QuoteSelectionContext, null=True, blank=True, on_delete=models.PROTECT, related_name="api_requests")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    objects = RevisionQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["actor", "request_id"], name="quote_api_request_unique"),
            models.CheckConstraint(condition=(Q(operation__in=["CREATE", "CLONE"], revision__isnull=False, context__isnull=True)
                | Q(operation="FX", revision__isnull=True, context__isnull=False)), name="quote_api_result_shape"),
            models.CheckConstraint(condition=Q(fingerprint__regex=r"^[0-9a-f]{64}$"), name="quote_api_fingerprint"),
        ]

    def clean(self):
        if self.context_id and (self.context.kind != "FX" or self.context.actor_id != self.actor_id):
            raise ValidationError("El recibo FX requiere un contexto propio de tipo FX.")
        if self.revision_id and self.revision.schema_version != 2:
            raise ValidationError("El recibo comercial requiere una revisión v2.")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("El recibo API es inmutable.")
        self.full_clean()
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("No borrar recibos de idempotencia.")


MAX_CONDICIONES_PREDETERMINADAS = 50  # mismo límite que acepta el cotizador


class DefaultCommercialCondition(models.Model):
    """M2: condición que el cotizador precarga en cada cotización nueva.

    Solo es un texto inicial que el asesor puede editar o borrar; cada revisión
    guarda su propia copia, así que cambiar esta lista no altera cotizaciones.
    """

    text = models.CharField("texto", max_length=6000)
    order = models.PositiveSmallIntegerField("orden", default=0)
    active = models.BooleanField("activa", default=True)

    class Meta:
        ordering = ["order", "pk"]
        verbose_name = "condición predeterminada"
        verbose_name_plural = "condiciones predeterminadas"

    def __str__(self):
        return self.text[:80]

    def clean(self):
        self.text = (self.text or "").strip()
        if not self.text:
            raise ValidationError({"text": "Escribe la condición."})
        if "\n" in self.text or "\r" in self.text:
            raise ValidationError({"text": "Una condición por registro, sin saltos de línea."})
        if self.active and DefaultCommercialCondition.objects.filter(active=True).exclude(pk=self.pk).count() >= MAX_CONDICIONES_PREDETERMINADAS:
            raise ValidationError(f"Máximo {MAX_CONDICIONES_PREDETERMINADAS} condiciones activas.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
