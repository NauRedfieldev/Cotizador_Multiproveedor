"""Modelos del catálogo de productos.

Contiene la clasificación (Category), el directorio de marcas/distribuidores
(Supplier) y los productos cotizables (Product). DER completo en docs/DER.md.
"""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


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
    """Marca o distribuidor del producto (ej. SYSCOM, CT ONLINE)."""

    name = models.CharField("nombre", max_length=100, unique=True)
    code = models.CharField("clave", max_length=20, unique=True, null=True, blank=True)
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
