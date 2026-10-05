"""Bloque D: modelos y restricciones de apps.providers."""
from contextlib import suppress

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import SimpleTestCase, TestCase, tag

from apps.providers.models import Provider, ProviderToken, RawProviderProduct
from apps.providers.tests.soporte import (
    CENTINELA_TOKEN,
    HASH_PAYLOAD,
    PAYLOAD,
    T0,
    T1,
    crear_proveedor,
)


def fila_cruda(proveedor, **campos):
    datos = {
        "provider": proveedor,
        "external_id": "X1",
        "content_hash": HASH_PAYLOAD,
        "payload": PAYLOAD,
        "first_seen_at": T0,
        "last_seen_at": T0,
    }
    datos.update(campos)
    return datos


@tag("django_db")
class PruebasProvider(TestCase):
    def test_code_de_proveedor_es_unico(self):
        """D1 Provider.code (el catálogo lo llama key) no admite duplicados."""
        # Arrange
        crear_proveedor("demo")

        # Act / Assert
        with self.assertRaisesMessage(IntegrityError, "providers_provider_code"), transaction.atomic():
            crear_proveedor("demo")

    def test_code_de_proveedor_solo_admite_slugs(self):
        """D1 Provider.code es un SlugField: la validación rechaza espacios y signos."""
        # Arrange
        proveedor = Provider(code="Sys Tecom!", name="Systecom", base_url="https://systecom.example.com")

        # Act
        with self.assertRaises(ValidationError) as contexto:
            proveedor.full_clean()

        # Assert
        self.assertIn("code", contexto.exception.message_dict)

    def test_proveedor_nuevo_esta_activo_por_defecto(self):
        """D2 Provider.active (el catálogo lo llama activo) vale True por defecto."""
        # Act
        proveedor = crear_proveedor()

        # Assert
        proveedor.refresh_from_db()
        self.assertIs(proveedor.active, True)

    def test_borrar_un_proveedor_con_historico_crudo_lanza_protected_error(self):
        """D10 Contrato (decisión 2): el histórico crudo está protegido (PROTECT)."""
        # Arrange
        proveedor = crear_proveedor("efimero")
        RawProviderProduct.objects.create(**fila_cruda(proveedor))

        # Act / Assert
        with self.assertRaises(ProtectedError):
            proveedor.delete()

    def test_un_borrado_rechazado_no_borra_el_proveedor_ni_su_historico_ni_su_token(self):
        """D10 Contrato (decisión 2): si el borrado se rechaza, no desaparece nada."""
        # Arrange
        proveedor = crear_proveedor("efimero")
        pk = proveedor.pk
        RawProviderProduct.objects.create(**fila_cruda(proveedor))
        ProviderToken.objects.create(provider=proveedor, access_token="t", expires_at=T1)

        # Act
        with suppress(ProtectedError):
            proveedor.delete()

        # Assert
        restantes = (
            Provider.objects.filter(pk=pk).count(),
            RawProviderProduct.objects.filter(provider_id=pk).count(),
            ProviderToken.objects.filter(provider_id=pk).count(),
        )
        self.assertEqual(restantes, (1, 1, 1))

    def test_borrar_un_proveedor_sin_historico_borra_tambien_su_token(self):
        """D10 Contrato (decisión 2): sin histórico crudo el borrado procede y el token cae en cascada."""
        # Arrange
        proveedor = crear_proveedor("efimero")
        pk = proveedor.pk
        ProviderToken.objects.create(provider=proveedor, access_token="t", expires_at=T1)

        # Act
        proveedor.delete()

        # Assert
        self.assertEqual(ProviderToken.objects.filter(provider_id=pk).count(), 0)


@tag("django_db")
class PruebasRawProviderProduct(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.proveedor = crear_proveedor()

    def test_fetch_count_vale_1_por_defecto(self):
        """D5 Una fila creada sin fetch_count empieza en 1."""
        # Act
        fila = RawProviderProduct.objects.create(**fila_cruda(self.proveedor))

        # Assert
        fila.refresh_from_db()
        self.assertEqual(fila.fetch_count, 1)

    def test_first_seen_at_no_tiene_valor_por_defecto_porque_lo_fija_aupsert(self):
        """D5 Contrato (decisión 5): first_seen_at lo fija aupsert; un create directo sin él falla."""
        # Arrange
        datos = fila_cruda(self.proveedor)
        del datos["first_seen_at"]

        # Act / Assert
        with self.assertRaisesMessage(IntegrityError, "first_seen_at"), transaction.atomic():
            RawProviderProduct.objects.create(**datos)


@tag("django_db")
class PruebasProviderToken(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.proveedor = crear_proveedor()
        cls.token = ProviderToken.objects.create(
            provider=cls.proveedor, access_token=CENTINELA_TOKEN, expires_at=T1
        )

    def test_str_de_provider_token_no_contiene_el_access_token(self):
        """D8 ⭐ str() de ProviderToken no expone el access_token."""
        # Act
        texto = str(self.token)

        # Assert
        self.assertNotIn(CENTINELA_TOKEN, texto)

    def test_repr_de_provider_token_no_contiene_el_access_token(self):
        """D8 ⭐ repr() de ProviderToken no expone el access_token."""
        # Act
        texto = repr(self.token)

        # Assert
        self.assertNotIn(CENTINELA_TOKEN, texto)

    def test_str_de_los_tres_modelos_devuelve_texto_con_instancias_validas(self):
        """D9 __str__ de Provider, RawProviderProduct y ProviderToken no lanza con instancias válidas."""
        # Arrange
        fila = RawProviderProduct(**fila_cruda(self.proveedor))

        # Act
        textos = [str(self.proveedor), str(fila), str(self.token)]

        # Assert
        self.assertEqual([type(texto) for texto in textos], [str, str, str])

    def test_str_de_provider_token_sin_expires_at_indica_que_no_hay_fecha(self):
        """D9 str() de un ProviderToken sin expires_at no lanza e indica que no hay fecha (HALLAZGO-2, corregido)."""
        # Arrange
        token = ProviderToken(provider=self.proveedor, access_token=CENTINELA_TOKEN)

        # Act
        texto = str(token)

        # Assert
        self.assertIn("sin fecha", texto)


@tag("unit")
class PruebasMetadatosDeModelos(SimpleTestCase):
    def test_nombres_de_tabla_de_los_modelos(self):
        """D4 db_table: raw_provider_product (explícito), providers_provider y providers_providertoken."""
        # Act
        tablas = [modelo._meta.db_table for modelo in (Provider, RawProviderProduct, ProviderToken)]

        # Assert
        self.assertEqual(tablas, ["providers_provider", "raw_provider_product", "providers_providertoken"])

    def test_campos_de_provider_token_son_exactamente_los_esperados(self):
        """D7 ProviderToken solo tiene id, provider, access_token, expires_at y obtained_at."""
        # Act
        campos = {campo.name for campo in ProviderToken._meta.concrete_fields}

        # Assert
        self.assertEqual(campos, {"id", "provider", "access_token", "expires_at", "obtained_at"})

    def test_app_providers_se_registra_con_la_ruta_completa(self):
        """D11 La app se registra como apps.providers con label providers."""
        # Act
        config = apps.get_app_config("providers")

        # Assert
        self.assertEqual((config.name, config.label), ("apps.providers", "providers"))

    def test_django_contrib_postgres_esta_instalado(self):
        """D11 django.contrib.postgres está en INSTALLED_APPS (lo exige el GinIndex)."""
        # Act / Assert
        self.assertTrue(apps.is_installed("django.contrib.postgres"))
