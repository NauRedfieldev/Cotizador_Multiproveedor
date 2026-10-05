"""SDD M2 §9, pruebas 27–42 (y ramas defensivas): la orquestación search_all."""
import asyncio
import dataclasses
import os
import time
from decimal import Decimal
from unittest import mock

import httpx
from django.test import TestCase, tag

from apps.providers import service
from apps.providers.adapters.base import ProviderAdapter
from apps.providers.contracts import ProductQuery
from apps.providers.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeout,
)
from apps.providers.models import Provider
from apps.providers.service import search_all
from apps.providers.tests.soporte import (
    CENTINELA_TOKEN,
    AdaptadorDePrueba,
    ApiFalsa,
    adaptadores_registrados,
    crear_adaptador,
    producto,
    productos,
    respuesta_json,
)

CONSULTA = ProductQuery(term="camara")
CENTINELA_CREDENCIAL = "credencial-centinela-qa-5b1e"


async def fetch_lento(self, query, client):
    await asyncio.sleep(1)
    return []


async def fetch_roto(self, query, client):
    raise RuntimeError("fallo del adaptador")


def parse_con_codigo_ajeno(self, item):
    oferta = AdaptadorDePrueba.parse_offer(self, item)
    return dataclasses.replace(oferta, provider_code="otro")


class AdaptadorConTokenSinSolicitud(ProviderAdapter):
    """Declara uses_token pero no implementa request_token."""

    code = "alfa"
    uses_token = True

    async def fetch(self, query, client):
        await self.token(client)
        return []

    def parse_offer(self, item):
        raise NotImplementedError


class BaseServicio(TestCase):
    def setUp(self):
        self.api = ApiFalsa()

    async def proveedor(self, code, **campos):
        return await Provider.objects.acreate(
            code=code, name=code.title(), base_url=f"https://{code}.example.com", **campos
        )

    async def buscar(self, consulta=CONSULTA):
        return await search_all(consulta, transport=self.api.transport)


@tag("django_db")
class PruebasConsultaCorrecta(BaseServicio):
    async def test_dos_proveedores_responden_en_orden_de_code_con_precios_decimal(self):
        """27 Dos proveedores activos responden: resultados ok, ordenados por code y con precios Decimal."""
        # Arrange
        await self.proveedor("beta")
        await self.proveedor("alfa")
        self.api.responder("alfa.example.com", "/productos", productos(producto("A1", precio="10.50")))
        self.api.responder("beta.example.com", "/productos", productos(producto("B1", precio="20.00")))

        # Act
        with adaptadores_registrados(crear_adaptador("alfa"), crear_adaptador("beta")):
            resultados = await self.buscar()

        # Assert
        self.assertEqual(
            [(r.provider_code, r.ok, [(o.price, type(o.price)) for o in r.offers]) for r in resultados],
            [
                ("alfa", True, [(Decimal("10.50"), Decimal)]),
                ("beta", True, [(Decimal("20.00"), Decimal)]),
            ],
        )


@tag("django_db")
class PruebasTiempos(BaseServicio):
    async def test_un_proveedor_lento_agota_su_timeout_sin_frenar_a_los_demas(self):
        """28 Un proveedor lento da ProviderTimeout, el otro responde y el total no espera al lento."""
        # Arrange
        await self.proveedor("lento", timeout_ms=50)
        await self.proveedor("alfa")
        self.api.responder("alfa.example.com", "/productos", productos(producto("A1")))
        inicio = time.perf_counter()

        # Act
        with adaptadores_registrados(crear_adaptador("alfa"), crear_adaptador("lento", fetch=fetch_lento)):
            resultados = await self.buscar()
        duracion = time.perf_counter() - inicio

        # Assert
        lento = resultados[1]
        self.assertEqual(
            (resultados[0].ok, type(lento.error), str(lento.error), duracion < 0.9),
            (True, ProviderTimeout, "[lento] Sin respuesta en 50 ms.", True),
        )

    async def test_el_cliente_http_no_tiene_timeout_propio(self):
        """§6.2 El httpx.AsyncClient se crea sin timeout: manda Provider.timeout_ms (el de httpx, 5 s, cortaría antes)."""
        # Arrange
        await self.proveedor("alfa")
        vistos = []

        async def fetch_que_mira_el_timeout(adaptador, query, client):
            vistos.append(client.timeout)
            return []

        # Act
        with adaptadores_registrados(crear_adaptador("alfa", fetch=fetch_que_mira_el_timeout)):
            await self.buscar()

        # Assert
        self.assertEqual(vistos, [httpx.Timeout(None)])

    async def test_un_timeout_de_httpx_tambien_es_provider_timeout(self):
        """29 Si httpx agota su tiempo (ReadTimeout), el resultado es ProviderTimeout."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder("alfa.example.com", "/productos", httpx.ReadTimeout)

        # Act
        with adaptadores_registrados(crear_adaptador("alfa")):
            resultados = await self.buscar()

        # Assert
        self.assertEqual(
            (type(resultados[0].error), str(resultados[0].error)),
            (ProviderTimeout, "[alfa] Sin respuesta en 8000 ms."),
        )


@tag("django_db")
class PruebasAutenticacion(BaseServicio):
    async def test_un_401_renueva_el_token_y_reintenta_una_vez(self):
        """30 Con uses_token, un 401 invalida el token, se pide otro y la segunda llamada va bien."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder(
            "alfa.example.com", "/token",
            respuesta_json({"access_token": "tok-1", "expires_in": 3600}),
            respuesta_json({"access_token": "tok-2", "expires_in": 3600}),
        )
        self.api.responder(
            "alfa.example.com", "/productos", httpx.Response(401), productos(producto("A1"))
        )

        # Act
        with adaptadores_registrados(crear_adaptador("alfa", uses_token=True)):
            resultados = await self.buscar()

        # Assert
        tokens_usados = [
            p.headers.get("Authorization") for p in self.api.peticiones if p.url.path == "/productos"
        ]
        self.assertEqual(
            (resultados[0].ok, self.api.rutas_pedidas(), tokens_usados),
            (
                True,
                ["alfa.example.com/token", "alfa.example.com/productos"] * 2,
                ["Bearer tok-1", "Bearer tok-2"],
            ),
        )

    async def test_dos_401_seguidos_dan_error_de_autenticacion(self):
        """31 Si tras renovar el token vuelve a dar 401, el resultado es ProviderAuthError(401)."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder(
            "alfa.example.com", "/token",
            respuesta_json({"access_token": "tok-1", "expires_in": 3600}),
            respuesta_json({"access_token": "tok-2", "expires_in": 3600}),
        )
        self.api.responder("alfa.example.com", "/productos", httpx.Response(401))

        # Act
        with adaptadores_registrados(crear_adaptador("alfa", uses_token=True)):
            resultados = await self.buscar()

        # Assert
        error = resultados[0].error
        self.assertEqual((type(error), error.status), (ProviderAuthError, 401))

    async def test_un_403_no_se_reintenta(self):
        """32 Un 403 es ProviderAuthError(403) y se hace una sola petición."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder("alfa.example.com", "/productos", httpx.Response(403))

        # Act
        with adaptadores_registrados(crear_adaptador("alfa")):
            resultados = await self.buscar()

        # Assert
        error = resultados[0].error
        self.assertEqual((type(error), error.status, len(self.api.peticiones)), (ProviderAuthError, 403, 1))

    async def test_un_403_con_token_no_se_reintenta(self):
        """§8 Un 403 (cuenta o scope) no se reintenta aunque el proveedor use token."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder(
            "alfa.example.com", "/token", respuesta_json({"access_token": "tok-1", "expires_in": 3600})
        )
        self.api.responder("alfa.example.com", "/productos", httpx.Response(403))

        # Act
        with adaptadores_registrados(crear_adaptador("alfa", uses_token=True)):
            resultados = await self.buscar()

        # Assert
        self.assertEqual(
            (resultados[0].error.status, self.api.rutas_pedidas()),
            (403, ["alfa.example.com/token", "alfa.example.com/productos"]),
        )

    async def test_un_401_sin_token_no_se_reintenta(self):
        """§8 Un 401 de un proveedor sin token no se reintenta: no hay token que renovar."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder("alfa.example.com", "/productos", httpx.Response(401))

        # Act
        with adaptadores_registrados(crear_adaptador("alfa")):
            resultados = await self.buscar()

        # Assert
        self.assertEqual((resultados[0].error.status, len(self.api.peticiones)), (401, 1))

    async def test_uses_token_sin_request_token_es_error_de_configuracion(self):
        """§8 Un adaptador que declara uses_token sin implementar request_token da ProviderConfigError."""
        # Arrange
        await self.proveedor("alfa")

        # Act
        with adaptadores_registrados(AdaptadorConTokenSinSolicitud):
            resultados = await self.buscar()

        # Assert
        self.assertEqual(str(resultados[0].error), "[alfa] El adaptador no implementa request_token.")


@tag("django_db")
class PruebasErroresHttp(BaseServicio):
    async def test_un_429_es_error_de_limite_sin_reintento(self):
        """33 Un 429 con Retry-After: 30 es ProviderRateLimitError(retry_after=30), sin reintento."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder(
            "alfa.example.com", "/productos", httpx.Response(429, headers={"Retry-After": "30"})
        )

        # Act
        with adaptadores_registrados(crear_adaptador("alfa")):
            resultados = await self.buscar()

        # Assert
        error = resultados[0].error
        self.assertEqual(
            (type(error), error.retry_after, len(self.api.peticiones)), (ProviderRateLimitError, 30, 1)
        )

    async def test_un_503_es_error_de_respuesta(self):
        """34 Un 503 es ProviderResponseError("HTTP 503 del proveedor.")."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder("alfa.example.com", "/productos", httpx.Response(503))

        # Act
        with adaptadores_registrados(crear_adaptador("alfa")):
            resultados = await self.buscar()

        # Assert
        self.assertEqual(str(resultados[0].error), "[alfa] HTTP 503 del proveedor.")

    async def test_un_error_de_conexion_es_error_de_respuesta(self):
        """37 Un httpx.ConnectError es ProviderResponseError("Error de conexión: ConnectError.")."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder("alfa.example.com", "/productos", httpx.ConnectError)

        # Act
        with adaptadores_registrados(crear_adaptador("alfa")):
            resultados = await self.buscar()

        # Assert
        self.assertEqual(str(resultados[0].error), "[alfa] Error de conexión: ConnectError.")


@tag("django_db")
class PruebasOfertasInvalidas(BaseServicio):
    async def test_una_oferta_invalida_se_descarta_y_se_cuenta(self):
        """35 De 3 productos con uno inválido salen 2 ofertas, discarded=1, el resultado es ok y queda un aviso."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder(
            "alfa.example.com", "/productos",
            productos(producto("A1"), producto("A2", precio="abc"), producto("A3")),
        )

        # Act
        with adaptadores_registrados(crear_adaptador("alfa")), \
                self.assertLogs("apps.providers", "WARNING") as registro:
            resultados = await self.buscar()

        # Assert
        resultado = resultados[0]
        self.assertEqual(
            ([o.external_id for o in resultado.offers], resultado.discarded, resultado.ok, len(registro.output)),
            (["A1", "A3"], 1, True, 1),
        )

    async def test_una_oferta_con_provider_code_ajeno_se_descarta(self):
        """36 Una oferta cuyo provider_code no es el del adaptador se descarta y se cuenta."""
        # Arrange
        await self.proveedor("alfa")
        self.api.responder("alfa.example.com", "/productos", productos(producto("A1")))

        # Act
        with adaptadores_registrados(crear_adaptador("alfa", parse_offer=parse_con_codigo_ajeno)), \
                self.assertLogs("apps.providers", "WARNING"):
            resultados = await self.buscar()

        # Assert
        self.assertEqual((resultados[0].offers, resultados[0].discarded), ((), 1))


@tag("django_db")
class PruebasFallosAislados(BaseServicio):
    async def test_una_excepcion_inesperada_no_afecta_a_los_demas_y_se_registra(self):
        """38 Un RuntimeError en un adaptador es "Error inesperado", se registra con traza y el otro sigue ok."""
        # Arrange
        await self.proveedor("alfa")
        await self.proveedor("beta")
        self.api.responder("beta.example.com", "/productos", productos(producto("B1")))

        # Act
        with adaptadores_registrados(crear_adaptador("alfa", fetch=fetch_roto), crear_adaptador("beta")), \
                self.assertLogs("apps.providers", "ERROR") as registro:
            resultados = await self.buscar()

        # Assert
        self.assertEqual(
            (str(resultados[0].error), resultados[1].ok, registro.records[0].exc_info is not None),
            ("[alfa] Error inesperado: RuntimeError.", True, True),
        )


@tag("django_db")
class PruebasConfiguracion(BaseServicio):
    async def test_un_proveedor_activo_sin_adaptador_es_error_de_configuracion(self):
        """39 Un proveedor activo sin adaptador registrado da ProviderConfigError."""
        # Arrange
        await self.proveedor("huerfano")

        # Act
        with adaptadores_registrados():
            resultados = await self.buscar()

        # Assert
        self.assertEqual(str(resultados[0].error), "[huerfano] No hay adaptador registrado.")

    async def test_un_proveedor_inactivo_no_se_consulta(self):
        """39 Un proveedor con active=False no aparece en los resultados."""
        # Arrange
        await self.proveedor("dormido", active=False)
        await self.proveedor("alfa")
        self.api.responder("alfa.example.com", "/productos", productos(producto("A1")))

        # Act
        with adaptadores_registrados(crear_adaptador("alfa"), crear_adaptador("dormido")):
            resultados = await self.buscar()

        # Assert
        self.assertEqual([r.provider_code for r in resultados], ["alfa"])

    async def test_sin_proveedores_activos_devuelve_lista_vacia_sin_peticiones(self):
        """39 Sin proveedores activos devuelve [] y no hace ninguna petición."""
        # Act
        resultados = await self.buscar()

        # Assert
        self.assertEqual((resultados, self.api.peticiones), ([], []))

    async def test_sin_credenciales_es_error_de_configuracion_sin_peticiones(self):
        """40 Si faltan las variables PROVIDER_<CODE>_*, da ProviderConfigError y no hace peticiones."""
        # Arrange
        await self.proveedor("alfa")
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("PROVIDER_ALFA_API_KEY", None)

        # Act
        with adaptadores_registrados(crear_adaptador("alfa", required_credentials=("API_KEY",))):
            resultados = await self.buscar()

        # Assert
        self.assertEqual(
            (str(resultados[0].error), self.api.peticiones),
            ("[alfa] Faltan credenciales: PROVIDER_ALFA_API_KEY", []),
        )


@tag("django_db")
class PruebasCancelacionYSecretos(BaseServicio):
    async def test_cancelar_la_busqueda_propaga_cancelled_error(self):
        """41 Si se cancela la tarea que ejecuta search_all, se propaga CancelledError."""
        # Arrange
        await self.proveedor("lento")
        self.enterContext(adaptadores_registrados(crear_adaptador("lento", fetch=fetch_lento)))
        tarea = asyncio.ensure_future(self.buscar())
        await asyncio.sleep(0.2)

        # Act
        tarea.cancel()

        # Assert
        with self.assertRaises(asyncio.CancelledError):
            await tarea

    async def test_los_logs_no_muestran_ni_el_token_ni_las_credenciales(self):
        """42 Ni el token ni las credenciales aparecen en los logs, ni siquiera al renovar o descartar."""
        # Arrange
        await self.proveedor("alfa")
        self.enterContext(mock.patch.dict(os.environ, {"PROVIDER_ALFA_CLIENT_SECRET": CENTINELA_CREDENCIAL}))
        self.api.responder(
            "alfa.example.com", "/token", respuesta_json({"access_token": CENTINELA_TOKEN, "expires_in": 3600})
        )
        self.api.responder(
            "alfa.example.com", "/productos",
            httpx.Response(401), productos(producto("A1"), producto("A2", precio="abc")),
        )
        adaptador = crear_adaptador("alfa", uses_token=True, required_credentials=("CLIENT_SECRET",))

        # Act
        with adaptadores_registrados(adaptador), self.assertLogs("apps.providers", "DEBUG") as registro:
            await self.buscar()

        # Assert
        texto = "\n".join(registro.output)
        self.assertEqual((CENTINELA_TOKEN in texto, CENTINELA_CREDENCIAL in texto), (False, False))


@tag("django_db")
class PruebasRamasDefensivas(BaseServicio):
    async def test_una_excepcion_que_escapa_de_la_consulta_se_convierte_en_resultado(self):
        """§6.2 Si una consulta lanzara fuera de su control, gather la devuelve y se convierte en error."""
        # Arrange
        await self.proveedor("alfa")
        self.enterContext(mock.patch.object(service, "_query_provider", side_effect=RuntimeError("x")))

        # Act
        with self.assertLogs("apps.providers", "ERROR"):
            resultados = await self.buscar()

        # Assert
        self.assertEqual(str(resultados[0].error), "[alfa] Error inesperado: RuntimeError.")

    async def test_una_cancelacion_devuelta_por_gather_se_relanza(self):
        """§6.2 Si gather devuelve un CancelledError de una consulta, search_all lo relanza."""
        # Arrange
        await self.proveedor("alfa")
        self.enterContext(
            mock.patch.object(service, "_query_provider", side_effect=asyncio.CancelledError())
        )

        # Act / Assert
        with self.assertRaises(asyncio.CancelledError):
            await self.buscar()
