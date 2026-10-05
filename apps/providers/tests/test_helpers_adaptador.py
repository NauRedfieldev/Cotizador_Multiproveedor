"""SDD M2 §9, pruebas 11–16: errores y funciones auxiliares de los adaptadores (sin BD)."""
import os
from decimal import Decimal
from unittest import mock

import httpx
from django.test import SimpleTestCase, tag

from apps.providers.adapters.base import (
    NewToken,
    ProviderAdapter,
    get_credentials,
    parse_json,
    parse_price,
    raise_for_status,
)
from apps.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeout,
)


def respuesta(status=200, texto="", cabeceras=None):
    return httpx.Response(
        status,
        text=texto,
        headers=cabeceras or {},
        request=httpx.Request("GET", "https://demo.example.com/productos"),
    )


class AdaptadorSinToken(ProviderAdapter):
    code = "demo"

    async def fetch(self, query, client):
        return []

    def parse_offer(self, item):
        raise NotImplementedError


@tag("unit")
class PruebasErrores(SimpleTestCase):
    def test_el_texto_de_un_error_incluye_el_proveedor(self):
        """11 str(error) es "[code] mensaje"."""
        # Act
        texto = str(ProviderTimeout("demo", "Sin respuesta en 50 ms."))

        # Assert
        self.assertEqual(texto, "[demo] Sin respuesta en 50 ms.")

    def test_el_error_de_autenticacion_guarda_el_status(self):
        """11 ProviderAuthError conserva el status HTTP."""
        # Act
        error = ProviderAuthError("demo", "Autenticación rechazada (HTTP 401).", status=401)

        # Assert
        self.assertEqual(error.status, 401)

    def test_el_error_de_limite_guarda_retry_after(self):
        """11 ProviderRateLimitError conserva los segundos de Retry-After."""
        # Act
        error = ProviderRateLimitError("demo", "Límite de peticiones superado (HTTP 429).", retry_after=30)

        # Assert
        self.assertEqual(error.retry_after, 30)


@tag("unit")
class PruebasCredenciales(SimpleTestCase):
    def test_lee_las_credenciales_del_entorno(self):
        """12 get_credentials devuelve cada clave pedida con su valor de PROVIDER_<CODE>_<CLAVE>."""
        # Arrange
        entorno = {"PROVIDER_DEMO_API_KEY": "valor-1"}

        # Act
        with mock.patch.dict(os.environ, entorno):
            credenciales = get_credentials("demo", ("API_KEY",))

        # Assert
        self.assertEqual(credenciales, {"API_KEY": "valor-1"})

    def test_si_falta_una_credencial_la_nombra_sin_mostrar_valores(self):
        """12 Si falta una variable, el error la nombra y no muestra el valor de las que sí están."""
        # Arrange
        entorno = {"PROVIDER_DEMO_CLIENT_ID": "valor-centinela"}

        # Act
        with mock.patch.dict(os.environ, entorno), self.assertRaises(ProviderConfigError) as contexto:
            get_credentials("demo", ("CLIENT_ID", "CLIENT_SECRET"))

        # Assert
        mensaje = str(contexto.exception)
        self.assertEqual(
            ("PROVIDER_DEMO_CLIENT_SECRET" in mensaje, "valor-centinela" in mensaje), (True, False)
        )

    def test_los_guiones_del_code_pasan_a_guion_bajo(self):
        """12 Con code="ct-online" la variable es PROVIDER_CT_ONLINE_<CLAVE>."""
        # Arrange
        entorno = {"PROVIDER_CT_ONLINE_API_KEY": "valor-2"}

        # Act
        with mock.patch.dict(os.environ, entorno):
            credenciales = get_credentials("ct-online", ("API_KEY",))

        # Assert
        self.assertEqual(credenciales, {"API_KEY": "valor-2"})


@tag("unit")
class PruebasParseJson(SimpleTestCase):
    def test_los_numeros_con_decimales_llegan_como_decimal(self):
        """13 parse_json convierte los números con decimales en Decimal, nunca en float."""
        # Act
        datos = parse_json("demo", respuesta(texto='{"price": 19.99}'))

        # Assert
        self.assertEqual((datos["price"], type(datos["price"])), (Decimal("19.99"), Decimal))

    def test_un_cuerpo_que_no_es_json_lanza_error_de_respuesta(self):
        """13 Un cuerpo que no es JSON se traduce a ProviderResponseError."""
        # Act / Assert
        with self.assertRaisesMessage(ProviderResponseError, "Respuesta no es JSON válido."):
            parse_json("demo", respuesta(texto="<html>"))

    def test_un_nan_en_el_json_lanza_error_de_respuesta(self):
        """13 NaN o Infinity no son JSON válido y se rechazan (si no, llegarían como float)."""
        # Act / Assert
        with self.assertRaisesMessage(ProviderResponseError, "Respuesta no es JSON válido."):
            parse_json("demo", respuesta(texto='{"price": NaN}'))


@tag("unit")
class PruebasParsePrice(SimpleTestCase):
    def test_acepta_un_decimal(self):
        """14 Un Decimal se devuelve tal cual."""
        # Act / Assert
        self.assertEqual(parse_price("demo", Decimal("1.10")), Decimal("1.10"))

    def test_acepta_un_entero(self):
        """14 Un entero se convierte en Decimal."""
        # Act / Assert
        self.assertEqual(parse_price("demo", 5), Decimal("5"))

    def test_acepta_un_texto_numerico(self):
        """14 Un texto numérico se convierte en Decimal conservando los ceros."""
        # Act / Assert
        self.assertEqual(parse_price("demo", "1.10"), Decimal("1.10"))

    def test_rechaza_un_float(self):
        """14 Un float se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ProviderResponseError, "Precio inválido: 1.1"):
            parse_price("demo", 1.1)

    def test_rechaza_un_bool(self):
        """14 Un bool se rechaza aunque en Python sea un int."""
        # Act / Assert
        with self.assertRaisesMessage(ProviderResponseError, "Precio inválido: True"):
            parse_price("demo", True)

    def test_rechaza_none(self):
        """14 None se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ProviderResponseError, "Precio inválido: None"):
            parse_price("demo", None)

    def test_rechaza_un_texto_no_numerico(self):
        """14 Un texto que no es un número se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ProviderResponseError, "Precio inválido: 'abc'"):
            parse_price("demo", "abc")

    def test_rechaza_nan(self):
        """14 "NaN" se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ProviderResponseError, "Precio inválido: 'NaN'"):
            parse_price("demo", "NaN")

    def test_rechaza_un_negativo(self):
        """14 Un precio negativo se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ProviderResponseError, "Precio inválido: '-3'"):
            parse_price("demo", "-3")


@tag("unit")
class PruebasRaiseForStatus(SimpleTestCase):
    def test_401_es_error_de_autenticacion_con_status(self):
        """15 401 → ProviderAuthError(status=401)."""
        # Act
        with self.assertRaises(ProviderAuthError) as contexto:
            raise_for_status("demo", respuesta(401))

        # Assert
        self.assertEqual(contexto.exception.status, 401)

    def test_403_es_error_de_autenticacion_con_status(self):
        """15 403 → ProviderAuthError(status=403)."""
        # Act
        with self.assertRaises(ProviderAuthError) as contexto:
            raise_for_status("demo", respuesta(403))

        # Assert
        self.assertEqual(contexto.exception.status, 403)

    def test_429_con_retry_after_en_segundos(self):
        """15 429 con Retry-After: 30 → ProviderRateLimitError(retry_after=30)."""
        # Act
        with self.assertRaises(ProviderRateLimitError) as contexto:
            raise_for_status("demo", respuesta(429, cabeceras={"Retry-After": "30"}))

        # Assert
        self.assertEqual(contexto.exception.retry_after, 30)

    def test_429_con_retry_after_en_formato_fecha(self):
        """15 429 con Retry-After en formato fecha → retry_after=None."""
        # Act
        with self.assertRaises(ProviderRateLimitError) as contexto:
            raise_for_status(
                "demo", respuesta(429, cabeceras={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
            )

        # Assert
        self.assertIsNone(contexto.exception.retry_after)

    def test_404_es_error_de_respuesta(self):
        """15 404 → ProviderResponseError("HTTP 404 del proveedor.")."""
        # Act / Assert
        with self.assertRaisesMessage(ProviderResponseError, "HTTP 404 del proveedor."):
            raise_for_status("demo", respuesta(404))

    def test_500_es_error_de_respuesta(self):
        """15 500 → ProviderResponseError("HTTP 500 del proveedor.")."""
        # Act / Assert
        with self.assertRaisesMessage(ProviderResponseError, "HTTP 500 del proveedor."):
            raise_for_status("demo", respuesta(500))

    def test_200_no_lanza(self):
        """15 Un 200 no lanza nada."""
        # Act / Assert
        self.assertIsNone(raise_for_status("demo", respuesta(200)))


@tag("unit")
class PruebasNewTokenYAdaptadorBase(SimpleTestCase):
    def test_el_repr_de_new_token_no_muestra_el_token(self):
        """16 repr(NewToken) no contiene el access_token."""
        # Act
        texto = repr(NewToken("secreto", 60))

        # Assert
        self.assertNotIn("secreto", texto)

    def test_new_token_rechaza_una_vida_no_positiva(self):
        """16 expires_in tiene que ser un entero positivo."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "expires_in debe ser un entero positivo."):
            NewToken("secreto", 0)

    def test_new_token_rechaza_un_token_vacio(self):
        """16 access_token no puede estar vacío."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "access_token no puede estar vacío."):
            NewToken("", 60)

    async def test_request_token_sin_implementar_es_error_de_configuracion(self):
        """§8 Un adaptador que no implementa request_token da ProviderConfigError."""
        # Arrange
        adaptador = AdaptadorSinToken(provider=None, credentials={})

        # Act / Assert
        with self.assertRaisesMessage(ProviderConfigError, "El adaptador no implementa request_token."):
            await adaptador.request_token(client=None)
