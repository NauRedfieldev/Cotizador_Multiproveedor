"""Fixtures sintéticos de identidad; no son datos reales de proveedores."""

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.test import TestCase

from apps.catalog.models import Category, Product, ProductoCanonico, Supplier
from apps.quotes.models import Quote, QuoteItem


class ProductoCanonicoTests(TestCase):
    def setUp(self):
        self.categoria = Category.objects.create(name="Redes sintéticas")

    def crear(self, **kwargs):
        datos = dict(marca="Marca X", modelo="ABC-24", mpn="ABC-24-P(C)",
                     sku_fabricante="FAB/123", descripcion="Cámara POE+ 2.0",
                     categoria=self.categoria)
        datos.update(kwargs)
        return ProductoCanonico.objects.create(**datos)

    def test_conserva_originales_y_deriva_campos(self):
        producto = self.crear()
        producto.refresh_from_db()
        self.assertEqual(producto.marca, "Marca X")
        self.assertEqual(producto.marca_norm, "MARCA X")
        self.assertEqual(producto.mpn, "ABC-24-P(C)")
        self.assertEqual(producto.mpn_norm, "ABC24PC")
        self.assertEqual(producto.modelo_norm, "ABC24")
        self.assertEqual(producto.sku_norm, "FAB123")
        self.assertEqual(producto.descripcion_norm, "CAMARA POE+ 2.0")
        self.assertIsNotNone(producto.created_at)

    def test_normalizacion_idempotente_y_sin_sinonimos(self):
        producto = self.crear(descripcion="sw 24p")
        inicial = (producto.marca_norm, producto.mpn_norm, producto.descripcion_norm)
        producto.save()
        self.assertEqual(inicial, (producto.marca_norm, producto.mpn_norm, producto.descripcion_norm))
        self.assertEqual(producto.descripcion_norm, "SW 24P")

    def test_identificadores_opcionales(self):
        producto = self.crear(mpn="", modelo="", sku_fabricante="")
        self.assertEqual((producto.mpn_norm, producto.modelo_norm, producto.sku_norm), ("", "", ""))

    def test_rechaza_campos_vacios_o_sin_informacion(self):
        for campos in [{"marca": ""}, {"marca": "   "}, {"descripcion": "@"}, {"mpn": "---"}, {"modelo": None}]:
            with self.subTest(campos=campos), self.assertRaises(ValidationError):
                self.crear(**campos)

    def test_no_trunca_identificadores(self):
        with self.assertRaises(ValidationError):
            self.crear(mpn="A" * 121)

    def test_variantes_y_colisiones_no_se_sobreescriben(self):
        a = self.crear(mpn="ABC-P(C)", descripcion="Equipo POE")
        b = self.crear(mpn="ABC-PC", descripcion="Equipo POE+")
        self.assertNotEqual(a.pk, b.pk)
        self.assertEqual(a.mpn_norm, b.mpn_norm)
        self.assertNotEqual(a.descripcion_norm, b.descripcion_norm)
        self.assertEqual(ProductoCanonico.objects.count(), 2)

    def test_duplicados_se_conservan_para_revision(self):
        self.crear()
        self.crear()
        self.assertEqual(ProductoCanonico.objects.count(), 2)

    def test_guardado_parcial_actualiza_derivados(self):
        producto = self.crear()
        producto.mpn = "NUEVO/12"
        producto.save(update_fields=["mpn"])
        producto.refresh_from_db()
        self.assertEqual(producto.mpn_norm, "NUEVO12")

    def test_categoria_protegida(self):
        self.crear()
        with self.assertRaises(ProtectedError):
            self.categoria.delete()

    def test_admin_y_ausencia_de_datos_comerciales(self):
        self.assertIn(ProductoCanonico, admin.site._registry)
        campos = {campo.name for campo in ProductoCanonico._meta.fields}
        self.assertTrue({"supplier", "provider", "unit_price", "currency", "stock"}.isdisjoint(campos))

    def test_product_y_quoteitem_sin_cambio_de_contrato(self):
        producto = Product.objects.create(
            sku="CAT-1", name="Producto existente", category=self.categoria,
            supplier=Supplier.objects.create(name="Distribuidor"), unit_price="15.00",
        )
        self.assertEqual(producto.sku, "CAT-1")
        self.assertEqual(QuoteItem._meta.get_field("product").related_model, Product)
        self.assertNotIn("canonico", {campo.name for campo in Product._meta.fields})
        self.assertNotIn("canonico", {campo.name for campo in Quote._meta.fields})
