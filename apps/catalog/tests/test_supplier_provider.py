"""Duda 7 del SDD de M2, alternativa A: Supplier se enlaza opcionalmente con providers.Provider."""
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import SimpleTestCase, TestCase, tag

from apps.catalog.models import Supplier
from apps.providers.contracts import ProviderOffer
from apps.providers.models import Provider


def crear_proveedor_api(code="syscom"):
    return Provider.objects.create(
        code=code, name=code.upper(), base_url=f"https://{code}.example.com"
    )


@tag("django_db")
class SupplierProviderTests(TestCase):
    def test_supplier_sin_proveedor_con_api_es_valido(self):
        supplier = Supplier.objects.create(name="Distribuidor local")
        supplier.full_clean()
        self.assertIsNone(supplier.provider)

    def test_varios_suppliers_sin_proveedor_con_api_conviven(self):
        Supplier.objects.create(name="Distribuidor A")
        Supplier.objects.create(name="Distribuidor B")
        self.assertEqual(Supplier.objects.filter(provider__isnull=True).count(), 2)

    def test_el_supplier_enlazado_se_alcanza_desde_el_proveedor(self):
        provider = crear_proveedor_api()
        supplier = Supplier.objects.create(name="SYSCOM", provider=provider)
        provider.refresh_from_db()
        self.assertEqual(provider.supplier, supplier)

    def test_un_proveedor_con_api_solo_enlaza_un_supplier(self):
        provider = crear_proveedor_api()
        Supplier.objects.create(name="SYSCOM", provider=provider)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Supplier.objects.create(name="SYSCOM bis", provider=provider)

    def test_no_se_puede_borrar_un_proveedor_enlazado_a_un_supplier(self):
        provider = crear_proveedor_api()
        Supplier.objects.create(name="SYSCOM", provider=provider)
        with self.assertRaises(ProtectedError):
            provider.delete()

    def test_borrar_el_supplier_no_borra_el_proveedor(self):
        provider = crear_proveedor_api()
        Supplier.objects.create(name="SYSCOM", provider=provider).delete()
        self.assertTrue(Provider.objects.filter(pk=provider.pk).exists())

    async def test_el_supplier_de_una_oferta_se_resuelve_por_su_provider_code(self):
        provider = await Provider.objects.acreate(
            code="syscom", name="SYSCOM", base_url="https://syscom.example.com"
        )
        supplier = await Supplier.objects.acreate(name="SYSCOM", provider=provider)
        oferta = ProviderOffer(
            provider_code="syscom", external_id="X1", name="Cámara IP",
            price=Decimal("10.50"), currency="USD",
        )
        self.assertEqual(await Supplier.objects.aget(provider__code=oferta.provider_code), supplier)


@tag("django_db")
class SupplierProviderSchemaTests(TestCase):
    def restricciones_sobre_provider_id(self):
        with connection.cursor() as cursor:
            restricciones = connection.introspection.get_constraints(cursor, "catalog_supplier")
        return [r for r in restricciones.values() if r["columns"] == ["provider_id"]]

    def test_la_columna_provider_id_admite_nulos(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'catalog_supplier' AND column_name = 'provider_id'"
            )
            self.assertEqual(cursor.fetchone(), ("YES",))

    def test_provider_id_es_unico(self):
        self.assertTrue(any(r["unique"] for r in self.restricciones_sobre_provider_id()))

    def test_provider_id_es_una_fk_a_providers_provider(self):
        destinos = [r["foreign_key"] for r in self.restricciones_sobre_provider_id() if r["foreign_key"]]
        self.assertEqual(destinos, [("providers_provider", "id")])


@tag("unit")
class DependencyDirectionTests(SimpleTestCase):
    def test_provider_no_declara_campos_hacia_catalog(self):
        hacia_catalog = [
            campo.name
            for campo in Provider._meta.concrete_fields
            if campo.related_model is not None and campo.related_model._meta.app_label == "catalog"
        ]
        self.assertEqual(hacia_catalog, [])
