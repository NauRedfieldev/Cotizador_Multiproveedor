"""Correcciones de la auditoría run-2 en proveedores (docs/37).

#5: base_url decide a dónde viajan credenciales de producción; solo el superusuario lo cambia, a un host
declarado en CCONOR_PROVIDER_HOSTS, y el token no sigue al host nuevo.
#18: una respuesta de proveedor de más de MAX_BYTES_RESPUESTA se corta (proveedor caído, nunca 500).
"""
from datetime import timedelta

import httpx
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone

from apps.providers.admin import ProviderForm
from apps.providers.contracts import ProductQuery
from apps.providers.errors import ProviderResponseError
from apps.providers.models import Provider, ProviderToken, validar_base_url
from apps.providers.service import MAX_BYTES_RESPUESTA, search_all
from apps.providers.tests.soporte import credenciales_de_prueba

SYSCOM_OFICIAL = "https://developers.syscom.mx/api/v1"


@tag("unit")
class ValidarBaseUrlTests(TestCase):
    def test_acepta_los_hosts_declarados(self):
        validar_base_url("syscom", SYSCOM_OFICIAL)
        validar_base_url("exel", "https://www.exel.com.mx/xlstore")
        validar_base_url("syscom", "https://DEVELOPERS.syscom.mx/api/v1/")

    def test_rechaza_destinos_que_desviarian_credenciales(self):
        for url in (
            "http://developers.syscom.mx/api/v1",            # texto plano
            "https://developers.syscom.mx:8443/api/v1",      # puerto explícito
            "https://developers.syscom.mx@evil.invalid/api",  # userinfo: el host real es evil.invalid
            "https://x:y@developers.syscom.mx/api/v1",       # credenciales en la URL
            "https://evil.invalid/api/v1",                   # host no declarado
            "https://169.254.169.254/latest/meta-data",      # metadatos de la nube
            "https://www.exel.com.mx/xlstore",               # host de otro proveedor
            "ftp://developers.syscom.mx/api/v1",
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                validar_base_url("syscom", url)

    def test_proveedor_sin_hosts_declarados(self):
        with self.assertRaisesMessage(ValidationError, "CCONOR_PROVIDER_HOSTS"):
            validar_base_url("nuevo", "https://api.nuevo.example.com")

    def test_el_formulario_del_admin_usa_el_validador(self):
        datos = {"code": "syscom", "name": "SYSCOM", "active": "on", "timeout_ms": "8000"}
        malo = ProviderForm(data={**datos, "base_url": "https://evil.invalid/api/v1"})
        self.assertFalse(malo.is_valid())
        self.assertIn("base_url", malo.errors)
        self.assertTrue(ProviderForm(data={**datos, "base_url": SYSCOM_OFICIAL}).is_valid())


class AdminBaseUrlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.proveedor = Provider.objects.create(code="syscom", name="SYSCOM", base_url=SYSCOM_OFICIAL)
        cls.superusuario = get_user_model().objects.create_superuser("qa_root", "root@example.invalid", password=None)
        cls.staff = get_user_model().objects.create_user("qa_staff_proveedores", is_staff=True)
        cls.staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="providers", codename__in=["view_provider", "change_provider", "add_provider"]))

    def setUp(self):
        self.token = ProviderToken.objects.create(provider=self.proveedor, access_token="token-ficticio",
                                                  expires_at=timezone.now() + timedelta(days=30))
        self.url = reverse("admin:providers_provider_change", args=[self.proveedor.pk])

    def datos(self, base_url):
        return {"code": "syscom", "name": "SYSCOM", "base_url": base_url, "active": "on", "timeout_ms": "8000"}

    def test_staff_no_superusuario_no_cambia_base_url(self):
        """Auditoría run-2 / #5 de la run-1: el POST del staff ya no redirige las credenciales."""
        self.client.force_login(self.staff)
        respuesta = self.client.post(self.url, self.datos("https://evil.invalid/api/v1"))
        self.assertEqual(respuesta.status_code, 302)  # guarda lo demás
        self.proveedor.refresh_from_db()
        self.assertEqual(self.proveedor.base_url, SYSCOM_OFICIAL)
        self.assertTrue(ProviderToken.objects.filter(pk=self.token.pk).exists())

    def test_staff_guarda_un_proveedor_con_base_url_anterior_a_la_lista(self):
        """Regresión: el validador no debe romper (500) el guardado del staff, que no ve base_url."""
        Provider.objects.filter(pk=self.proveedor.pk).update(base_url="https://api.syscom.mx/api/v1")
        self.client.force_login(self.staff)
        respuesta = self.client.post(self.url, {**self.datos("ignorado"), "timeout_ms": "9000"})
        self.assertEqual(respuesta.status_code, 302)
        self.proveedor.refresh_from_db()
        self.assertEqual((self.proveedor.base_url, self.proveedor.timeout_ms), ("https://api.syscom.mx/api/v1", 9000))

    def test_staff_no_superusuario_no_da_de_alta_proveedores(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("admin:providers_provider_add")).status_code, 403)

    def test_superusuario_con_host_no_declarado_ve_el_error(self):
        self.client.force_login(self.superusuario)
        respuesta = self.client.post(self.url, self.datos("https://developers.syscom.mx@evil.invalid/api"))
        self.assertEqual(respuesta.status_code, 200)  # formulario con errores
        self.proveedor.refresh_from_db()
        self.assertEqual(self.proveedor.base_url, SYSCOM_OFICIAL)

    def test_cambiar_base_url_borra_el_token(self):
        self.client.force_login(self.superusuario)
        respuesta = self.client.post(self.url, self.datos("https://developers.syscom.mx/api/v2"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertFalse(ProviderToken.objects.filter(provider=self.proveedor).exists())

    def test_guardar_sin_cambiar_base_url_conserva_el_token(self):
        self.client.force_login(self.superusuario)
        self.client.post(self.url, {**self.datos(SYSCOM_OFICIAL), "timeout_ms": "9000"})
        self.assertTrue(ProviderToken.objects.filter(pk=self.token.pk).exists())


class _Trozos(httpx.AsyncByteStream):
    """Respuesta sin Content-Length (chunked): cuenta cuántos trozos llegaron a pedirse."""

    def __init__(self, trozos, tamano):
        self.trozos, self.tamano, self.entregados = trozos, tamano, 0

    async def __aiter__(self):
        for _ in range(self.trozos):
            self.entregados += 1
            yield b"A" * self.tamano


class TopeDeRespuestaTests(TestCase):
    """#18: el núcleo leía la respuesta entera en memoria (60 MB pasaron sin error)."""

    async def buscar(self, handler):
        p = await Provider.objects.acreate(code="syscom", name="SYSCOM", base_url=SYSCOM_OFICIAL)
        await ProviderToken.objects.acreate(provider=p, access_token="token-ficticio",
                                            expires_at=timezone.now() + timedelta(days=30))
        with credenciales_de_prueba("syscom", CLIENT_ID="id-ficticio", CLIENT_SECRET="secreto-ficticio"):
            return (await search_all(ProductQuery(term="camara"), transport=httpx.MockTransport(handler)))[0]

    async def test_respuesta_con_content_length_enorme_se_corta(self):
        resultado = await self.buscar(lambda r: httpx.Response(200, content=b"A" * (MAX_BYTES_RESPUESTA + 1)))
        self.assertIsInstance(resultado.error, ProviderResponseError)
        self.assertIn("demasiado grande", str(resultado.error))
        self.assertEqual(resultado.offers, ())

    async def test_respuesta_chunked_se_corta_sin_leerla_entera(self):
        cuerpo = _Trozos(trozos=60, tamano=1024 * 1024)
        resultado = await self.buscar(lambda r: httpx.Response(200, stream=cuerpo))
        self.assertIn("demasiado grande", str(resultado.error))
        self.assertLessEqual(cuerpo.entregados, MAX_BYTES_RESPUESTA // (1024 * 1024) + 1)

    async def test_respuesta_normal_pasa(self):
        resultado = await self.buscar(lambda r: httpx.Response(200, json={"productos": []}))
        self.assertIsNone(resultado.error)
