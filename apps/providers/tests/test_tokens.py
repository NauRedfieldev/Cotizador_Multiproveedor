"""SDD M2 §9, pruebas 21–26: token OAuth2 guardado en ProviderToken."""
import asyncio
from datetime import timedelta

from django.test import TestCase, tag

from apps.providers.adapters.base import NewToken
from apps.providers.errors import ProviderAuthError
from apps.providers.models import ProviderToken
from apps.providers.tests.soporte import T0, RelojControlado, crear_proveedor
from apps.providers.tokens import get_token, invalidate_token


class SolicitudFalsa:
    """Sustituye a adapter.request_token: cuenta las llamadas y devuelve un NewToken."""

    def __init__(self, token="tok-nuevo", expires_in=3600, error=None, espera=0):
        self.token = token
        self.expires_in = expires_in
        self.error = error
        self.espera = espera
        self.llamadas = 0

    async def __call__(self):
        self.llamadas += 1
        if self.espera:
            await asyncio.sleep(self.espera)
        if self.error:
            raise self.error
        return NewToken(self.token, self.expires_in)


@tag("django_db")
class PruebasTokens(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.proveedor = crear_proveedor()

    def setUp(self):
        self.reloj = self.enterContext(RelojControlado(T0))

    async def guardar_token(self, token, vence_en):
        await ProviderToken.objects.acreate(
            provider=self.proveedor, access_token=token, expires_at=T0 + vence_en
        )

    async def test_sin_token_guardado_pide_uno_y_lo_devuelve(self):
        """21 Sin ProviderToken se pide un token una vez y se devuelve."""
        # Arrange
        solicitud = SolicitudFalsa(token="tok-nuevo")

        # Act
        token = await get_token(self.proveedor, solicitud)

        # Assert
        self.assertEqual((token, solicitud.llamadas), ("tok-nuevo", 1))

    async def test_el_token_nuevo_se_guarda_con_su_caducidad(self):
        """21 El token nuevo se guarda con expires_at = ahora + expires_in y obtained_at = ahora."""
        # Act
        await get_token(self.proveedor, SolicitudFalsa(token="tok-nuevo", expires_in=3600))

        # Assert
        guardado = await ProviderToken.objects.aget(provider=self.proveedor)
        self.assertEqual(
            (guardado.access_token, guardado.expires_at, guardado.obtained_at),
            ("tok-nuevo", T0 + timedelta(seconds=3600), T0),
        )

    async def test_un_token_que_vence_despues_del_margen_se_reutiliza(self):
        """22 Un token que vence en más de 5 minutos se reutiliza sin pedir otro."""
        # Arrange
        await self.guardar_token("tok-guardado", timedelta(minutes=10))
        solicitud = SolicitudFalsa()

        # Act
        token = await get_token(self.proveedor, solicitud)

        # Assert
        self.assertEqual((token, solicitud.llamadas), ("tok-guardado", 0))

    async def test_un_token_que_vence_dentro_del_margen_se_renueva(self):
        """23 Un token que vence en 4 minutos (dentro del margen de 5) se renueva."""
        # Arrange
        await self.guardar_token("tok-viejo", timedelta(minutes=4))
        solicitud = SolicitudFalsa(token="tok-nuevo")

        # Act
        token = await get_token(self.proveedor, solicitud)

        # Assert
        self.assertEqual((token, solicitud.llamadas), ("tok-nuevo", 1))

    async def test_invalidar_conserva_la_fila_y_la_marca_como_caducada(self):
        """24 invalidate_token no borra la fila: pone expires_at = ahora."""
        # Arrange
        await self.guardar_token("tok-guardado", timedelta(days=365))

        # Act
        await invalidate_token(self.proveedor)

        # Assert
        filas = [fila.expires_at async for fila in ProviderToken.objects.filter(provider=self.proveedor)]
        self.assertEqual(filas, [T0])

    async def test_tras_invalidar_se_pide_un_token_nuevo(self):
        """24 Después de invalidar, la siguiente llamada pide un token nuevo."""
        # Arrange
        await self.guardar_token("tok-guardado", timedelta(days=365))
        await invalidate_token(self.proveedor)
        solicitud = SolicitudFalsa(token="tok-nuevo")

        # Act
        token = await get_token(self.proveedor, solicitud)

        # Assert
        self.assertEqual((token, solicitud.llamadas), ("tok-nuevo", 1))

    async def test_dos_peticiones_simultaneas_piden_un_solo_token(self):
        """25 Dos get_token a la vez sobre el mismo proveedor llaman a request_token una sola vez."""
        # Arrange
        solicitud = SolicitudFalsa(token="tok-nuevo", espera=0.05)

        # Act
        tokens = await asyncio.gather(
            get_token(self.proveedor, solicitud), get_token(self.proveedor, solicitud)
        )

        # Assert
        self.assertEqual((tokens, solicitud.llamadas), (["tok-nuevo", "tok-nuevo"], 1))

    async def test_si_la_solicitud_falla_el_error_se_propaga_y_no_se_guarda_nada(self):
        """26 Un rechazo de credenciales se propaga tal cual y no deja ningún ProviderToken."""
        # Arrange
        rechazo = ProviderAuthError("demo", "Autenticación rechazada (HTTP 401).", status=401)

        # Act
        with self.assertRaises(ProviderAuthError):
            await get_token(self.proveedor, SolicitudFalsa(error=rechazo))

        # Assert
        self.assertFalse(await ProviderToken.objects.filter(provider=self.proveedor).aexists())
