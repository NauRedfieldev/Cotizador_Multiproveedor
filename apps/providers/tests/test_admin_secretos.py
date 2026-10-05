"""Bloque F: admin de providers y exposición de secretos."""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, tag
from django.urls import reverse

from apps.providers.models import ProviderToken, RawProviderProduct
from apps.providers.tests.soporte import (
    CENTINELA_TOKEN,
    HASH_PAYLOAD,
    PAYLOAD,
    T0,
    T1,
    crear_proveedor,
)


class BaseAdmin(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Sin contraseña: force_login no la necesita y así no se paga el hash en cada clase.
        cls.superusuario = get_user_model().objects.create_superuser(
            "qa_admin", "qa@example.com", password=None
        )
        cls.proveedor = crear_proveedor()
        cls.fila = RawProviderProduct.objects.create(
            provider=cls.proveedor,
            external_id="X1",
            content_hash=HASH_PAYLOAD,
            payload=PAYLOAD,
            first_seen_at=T0,
            last_seen_at=T0,
        )
        cls.token = ProviderToken.objects.create(
            provider=cls.proveedor, access_token=CENTINELA_TOKEN, expires_at=T1
        )

    def setUp(self):
        self.client.force_login(self.superusuario)

    def peticion_de_superusuario(self):
        peticion = RequestFactory().get("/admin/")
        peticion.user = self.superusuario
        return peticion


@tag("django_db")
class PruebasAdminRawProviderProduct(BaseAdmin):
    def test_la_pagina_de_alta_responde_403(self):
        """F1 ⭐ El admin de RawProviderProduct no permite dar de alta filas (403)."""
        # Act
        respuesta = self.client.get(reverse("admin:providers_rawproviderproduct_add"))

        # Assert
        self.assertEqual(respuesta.status_code, 403)

    def test_no_permite_editar_filas(self):
        """F1 ⭐ El admin de RawProviderProduct no permite editar (HALLAZGO-1, corregido)."""
        # Arrange
        modelo_admin = admin.site.get_model_admin(RawProviderProduct)

        # Act
        puede_editar = modelo_admin.has_change_permission(self.peticion_de_superusuario(), self.fila)

        # Assert
        self.assertFalse(puede_editar)

    def test_no_permite_borrar_filas(self):
        """F1 ⭐ El admin de RawProviderProduct no permite borrar, tampoco en masa (HALLAZGO-1, corregido)."""
        # Arrange
        modelo_admin = admin.site.get_model_admin(RawProviderProduct)

        # Act
        puede_borrar = modelo_admin.has_delete_permission(self.peticion_de_superusuario(), self.fila)

        # Assert
        self.assertFalse(puede_borrar)


@tag("django_db")
class PruebasAdminProviderToken(BaseAdmin):
    def test_el_listado_de_tokens_no_muestra_el_access_token(self):
        """F2 ⭐ El listado de ProviderToken responde 200 y su HTML no contiene el token."""
        # Act
        respuesta = self.client.get(reverse("admin:providers_providertoken_changelist"))

        # Assert
        self.assertEqual(
            (respuesta.status_code, CENTINELA_TOKEN in respuesta.content.decode()), (200, False)
        )

    def test_la_edicion_de_un_token_no_muestra_el_access_token(self):
        """F2 ⭐ La página de edición de ProviderToken responde 200 y su HTML no contiene el token."""
        # Act
        respuesta = self.client.get(
            reverse("admin:providers_providertoken_change", args=[self.token.pk])
        )

        # Assert
        self.assertEqual(
            (respuesta.status_code, CENTINELA_TOKEN in respuesta.content.decode()), (200, False)
        )

    def test_access_token_no_figura_en_la_configuracion_del_admin(self):
        """F3 access_token no aparece en list_display, fields, readonly_fields ni search_fields."""
        # Arrange
        modelo_admin = admin.site.get_model_admin(ProviderToken)
        peticion = self.peticion_de_superusuario()

        # Act
        configurados = {
            *modelo_admin.get_list_display(peticion),
            *modelo_admin.get_fields(peticion, self.token),
            *modelo_admin.get_readonly_fields(peticion, self.token),
            *modelo_admin.get_search_fields(peticion),
        }

        # Assert
        self.assertNotIn("access_token", configurados)


@tag("django_db")
class PruebasRegistroEnAdmin(BaseAdmin):
    def test_los_tres_modelos_tienen_listado_en_el_admin(self):
        """F4 Provider, RawProviderProduct y ProviderToken están registrados y su listado responde 200."""
        # Act
        estados = [
            self.client.get(reverse(f"admin:providers_{modelo}_changelist")).status_code
            for modelo in ("provider", "rawproviderproduct", "providertoken")
        ]

        # Assert
        self.assertEqual(estados, [200, 200, 200])
