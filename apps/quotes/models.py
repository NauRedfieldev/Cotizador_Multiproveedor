"""Modelos de cotizaciones: cabecera (Quote) y partidas (QuoteItem).

DER completo y diccionario de datos en docs/DER.md.
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Sum
from django.utils import timezone

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
