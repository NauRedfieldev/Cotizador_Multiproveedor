"""Modelos del catálogo de productos.

Contiene la clasificación (Category), el directorio de marcas/distribuidores
(Supplier) y los productos cotizables (Product). DER completo en docs/DER.md.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .normalizer import normalizar


class Category(models.Model):
    """Clasificación de productos (ej. Cableado estructurado, Redes)."""

    name = models.CharField("nombre", max_length=100, unique=True)
    description = models.TextField("descripción", blank=True)
    is_active = models.BooleanField("activa", default=True)
    created_at = models.DateTimeField("creada", auto_now_add=True)
    updated_at = models.DateTimeField("actualizada", auto_now=True)

    class Meta:
        verbose_name = "categoría"
        verbose_name_plural = "categorías"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Supplier(models.Model):
    """Marca o distribuidor del producto (ej. SYSCOM, CT ONLINE).

    Si el distribuidor se consulta por API, `provider` lo enlaza con su configuración en
    apps.providers (duda 7 del SDD de M2, alternativa A). La dependencia va de catalog a
    providers, nunca al revés.
    """

    name = models.CharField("nombre", max_length=100, unique=True)
    code = models.CharField("clave", max_length=20, unique=True, null=True, blank=True)
    provider = models.OneToOneField(
        "providers.Provider",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier",
        verbose_name="proveedor con API",
        help_text="Solo si este distribuidor se consulta por API (apps.providers).",
    )
    email = models.EmailField("correo", blank=True)
    phone = models.CharField("teléfono", max_length=20, blank=True)
    website = models.URLField("sitio web", blank=True)
    is_active = models.BooleanField("activo", default=True)
    created_at = models.DateTimeField("creado", auto_now_add=True)
    updated_at = models.DateTimeField("actualizado", auto_now=True)

    class Meta:
        verbose_name = "proveedor"
        verbose_name_plural = "proveedores"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Product(models.Model):
    """Producto cotizable del catálogo.

    `unit_price` es el precio base vigente. Al cotizar, QuoteItem congela
    una copia (snapshot) para que cambios de precio posteriores no alteren
    cotizaciones ya emitidas.
    """

    sku = models.CharField("SKU / clave", max_length=50, unique=True)
    name = models.CharField("nombre", max_length=200)
    description = models.TextField("descripción detallada", blank=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="products",
        verbose_name="categoría",
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="products",
        verbose_name="proveedor",
    )
    unit_price = models.DecimalField(
        "precio unitario base",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    is_active = models.BooleanField("activo", default=True)
    created_at = models.DateTimeField("creado", auto_now_add=True)
    updated_at = models.DateTimeField("actualizado", auto_now=True)

    class Meta:
        verbose_name = "producto"
        verbose_name_plural = "productos"
        ordering = ["sku"]

    def __str__(self):
        return f"{self.sku} — {self.name}"


class SinonimoRed(models.Model):
    """Sustitución de un token completo por una expansión aprobada."""

    abreviatura = models.CharField("abreviatura", max_length=40, unique=True)
    expansion = models.CharField("expansión", max_length=120)

    class Meta:
        verbose_name = "sinónimo de red"
        verbose_name_plural = "sinónimos de red"
        ordering = ["abreviatura"]

    def clean_fields(self, exclude=None):
        # Antes de validar longitud/unicidad (también desde el admin).
        exclude = set(exclude or ())
        for campo in ("abreviatura", "expansion"):
            if campo not in exclude and isinstance(getattr(self, campo), str):
                setattr(self, campo, normalizar(getattr(self, campo)))
        super().clean_fields(exclude=exclude)
        if "abreviatura" not in exclude and " " in self.abreviatura:
            raise ValidationError({"abreviatura": "Debe ser un único token."})

    def save(self, *args, **kwargs):
        # La restricción única de BD resuelve también escrituras concurrentes.
        self.full_clean(validate_unique=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.abreviatura} → {self.expansion}"


@receiver(post_save, sender=SinonimoRed)
@receiver(post_delete, sender=SinonimoRed)
def _invalidar_sinonimos(sender, using, **kwargs):
    from .synonyms import invalidar_cache

    invalidar_cache()
    # Una lectura concurrente antes del commit podría haber recargado la copia vieja.
    transaction.on_commit(invalidar_cache, using=using)
