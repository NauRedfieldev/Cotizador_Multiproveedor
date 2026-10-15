"""Cotizaciones legacy y revisiones/snapshots M8 con sellado inmutable.

DER completo y diccionario de datos en docs/DER.md.
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from .snapshot_values import decimal_limit
from .storage import QuotePDFStorage, quote_pdf_path

TWO_PLACES = Decimal("0.01")
DEFAULT_TAX_RATE = Decimal("0.16")  # IVA México


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
        """Genera folio correlativo COT-YYYY-XXXX (el consecutivo reinicia cada año).

        La unicidad la garantiza el constraint `unique` en BD; ante una
        colisión por concurrencia basta reintentar el guardado.
        """
        prefix = f"COT-{timezone.localdate().year}-"
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
        ]

    def clean(self):
        self.tax_rate = decimal_limit(self.tax_rate, 5, 4, maximum=Decimal("1"))
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

    def clean_fields(self, exclude=None):
        self.tax_rate = decimal_limit(self.tax_rate, 5, 4, maximum=Decimal("1"))
        if self.exchange_rate is not None:
            self.exchange_rate = decimal_limit(self.exchange_rate, 18, 8, minimum=Decimal("0.00000001"))
        for field in ("subtotal", "tax", "total"):
            if getattr(self, field) is not None:
                setattr(self, field, decimal_limit(getattr(self, field), 38, 2))
        super().clean_fields(exclude=exclude)

    def save(self, *args, _seal=False, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.select_for_update().get(pk=self.pk)
                if previous.status == self.Status.ISSUED:
                    raise ValidationError("La revisión emitida es inmutable; crea otra revisión.")
                if any(getattr(previous, f) != getattr(self, f) for f in (
                    "quote_id", "number", "based_on_id", "request_id", "request_fingerprint", "schema_version",
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
    source_unit_price = models.DecimalField(max_digits=18, decimal_places=6)
    source_currency = models.CharField(max_length=3, choices=[("MXN", "MXN"), ("USD", "USD")])
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.PositiveBigIntegerField(null=True, blank=True)
    es_mejor_precio = models.BooleanField(default=False)
    amount = models.DecimalField(max_digits=38, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(default=timezone.now)

    objects = RevisionQuerySet.as_manager()

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["revision", "position"], name="quote_snapshot_position_unique"),
            models.CheckConstraint(condition=Q(source_unit_price__gte=0, quantity__gt=0, position__gte=1),
                                   name="quote_snapshot_values_positive"),
            models.CheckConstraint(condition=Q(source_currency__in=["MXN", "USD"]), name="quote_snapshot_currency_supported"),
        ]

    def clean(self):
        self.source_unit_price = decimal_limit(self.source_unit_price, 18, 6)
        self.quantity = decimal_limit(self.quantity, 10, 2, minimum=Decimal("0.01"))
        if self.stock is not None and (type(self.stock) is not int or self.stock < 0):
            raise ValidationError("Stock debe ser entero no negativo o None.")
        if bool(self.provider_code) != bool(self.external_id) or (self.provider_code and not self.provider_name):
            raise ValidationError("La oferta requiere proveedor, nombre e ID externo coherentes.")

    def clean_fields(self, exclude=None):
        self.source_unit_price = decimal_limit(self.source_unit_price, 18, 6)
        self.quantity = decimal_limit(self.quantity, 10, 2, minimum=Decimal("0.01"))
        if self.stock is not None and (type(self.stock) is not int or not 0 <= self.stock <= 9223372036854775807):
            raise ValidationError("Stock fuera de rango o no entero.")
        if type(self.es_mejor_precio) is not bool:
            raise ValidationError("es_mejor_precio debe ser bool.")
        if self.amount is not None:
            self.amount = decimal_limit(self.amount, 38, 2)
        super().clean_fields(exclude=exclude)

    def save(self, *args, _seal=False, **kwargs):
        with transaction.atomic():
            parent = QuoteRevision.objects.select_for_update().get(pk=self.revision_id)
            if parent.status == QuoteRevision.Status.ISSUED:
                raise ValidationError("No se modifica una partida emitida.")
            if self.pk and type(self).objects.get(pk=self.pk).revision_id != self.revision_id:
                raise ValidationError("No se mueve una partida a otra revisión.")
            if self.source_currency == "USD" and parent.exchange_rate is None:
                raise ValidationError("USD requiere tipo de cambio explícito.")
            if self.amount is not None and not _seal:
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
