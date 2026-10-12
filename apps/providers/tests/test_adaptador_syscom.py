"""SDD M3 §9, pruebas S1–S27: adaptador de SYSCOM.

Sin red y sin credenciales reales (docs/16 §6.2): ApiFalsa, sin_red() y credenciales_de_prueba().
Las muestras de muestras/ tienen la estructura real de la API (consultada el 2026-10-05) con
valores ficticios.
"""
import copy
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import httpx
from django.test import SimpleTestCase, TestCase, tag
from django.utils import timezone

from apps.providers.adapters.base import NewToken
from apps.providers.adapters.syscom import SyscomAdapter
from apps.providers.contracts import ProductQuery, ProviderOffer
from apps.providers.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from apps.providers.models import Provider, ProviderToken
from apps.providers.registry import get_adapter_class, registered_codes
from apps.providers.service import search_all
from apps.providers.tests.soporte import (
    CENTINELA_TOKEN,
    ApiFalsa,
    credenciales_de_prueba,
    exigir_bd_de_pruebas,
    respuesta_json,
    sin_red,
)

MUESTRAS = Path(__file__).parent / "muestras"
HOST = "developers.syscom.mx"
BASE_URL = "https://developers.syscom.mx/api/v1"
RUTA_TOKEN = "/api/v1/oauth/token"
RUTA_PRODUCTOS = "/api/v1/productos"
TOKEN = f"{HOST}{RUTA_TOKEN}"
PRODUCTOS = f"{HOST}{RUTA_PRODUCTOS}"
TOKEN_MUESTRA = "token-de-muestra-no-real"
CLIENT_ID_FALSO = "id-de-prueba"
CENTINELA_SECRETO = "secreto-centinela-qa-3c9d-no-debe-aparecer"
# Lo que el núcleo descarta cuando parse_offer falla (service._OFERTA_INVALIDA).
OFERTA_INVALIDA = (ProviderResponseError, ValueError, TypeError, KeyError)


def muestra(nombre):
    return json.loads((MUESTRAS / f"{nombre}.json").read_text(encoding="utf-8"))


def respuesta_muestra(nombre, status=200):
    """Sirve la muestra como texto: los precios pasan por parse_json igual que con la API real."""
    contenido = (MUESTRAS / f"{nombre}.json").read_bytes()
    return httpx.Response(status, content=contenido, headers={"content-type": "application/json"})


def producto_muestra(indice=0):
    return copy.deepcopy(muestra("syscom_productos")["productos"][indice])


def adaptador(base_url=BASE_URL):
    proveedor = Provider(code="syscom", name="SYSCOM", base_url=base_url)
    return SyscomAdapter(proveedor, {"CLIENT_ID": CLIENT_ID_FALSO, "CLIENT_SECRET": CENTINELA_SECRETO})


@tag("unit")
class RedDeSeguridadTests(SimpleTestCase):
    def test_sin_red_corta_una_peticion_real_y_hace_fallar_la_prueba(self):
        """S1 sin_red() corta el transporte real síncrono y, al salir, hace fallar la prueba."""
        # Act / Assert
        with self.assertRaisesMessage(AssertionError, "Una prueba intentó salir a la red"):
            with sin_red() as intentos:
                with self.assertRaises(httpx.ConnectError):
                    httpx.get("https://ejemplo.invalid/productos")
                self.assertEqual(intentos, ["GET ejemplo.invalid"])

    async def test_sin_red_tambien_corta_el_transporte_async(self):
        """S1 sin_red() corta también el transporte real asíncrono, el que usa search_all."""
        # Act / Assert
        with self.assertRaisesMessage(AssertionError, "Una prueba intentó salir a la red"):
            with sin_red():
                async with httpx.AsyncClient() as cliente:
                    with self.assertRaises(httpx.ConnectError):
                        await cliente.get("https://ejemplo.invalid/productos")

    def test_sin_red_no_falla_si_nadie_sale_a_la_red(self):
        """S1 Si nadie usa un transporte real, sin_red() no interfiere (ApiFalsa sigue funcionando)."""
        # Arrange
        api = ApiFalsa()
        api.responder("ejemplo.invalid", "/productos", respuesta_json({"ok": True}))

        # Act
        with sin_red() as intentos:
            respuesta = httpx.Client(transport=httpx.MockTransport(api)).get("https://ejemplo.invalid/productos")

        # Assert
        self.assertEqual((respuesta.json(), intentos), ({"ok": True}, []))

    def test_credenciales_de_prueba_ocultan_las_reales_y_las_restauran(self):
        """S2 Dentro de credenciales_de_prueba() solo se ven las falsas; al salir vuelven las reales."""
        # Arrange
        reales = {"PROVIDER_SYSCOM_CLIENT_ID": "real-1", "CLIENT_SECRET_SYSCOM": "real-2"}
        with mock.patch.dict(os.environ, reales):
            # Act
            with credenciales_de_prueba("syscom", CLIENT_ID="falso"):
                visibles = {k: v for k, v in os.environ.items() if "SYSCOM" in k.upper()}
            despues = {k: os.environ.get(k) for k in reales}

        # Assert
        self.assertEqual((visibles, despues), ({"PROVIDER_SYSCOM_CLIENT_ID": "falso"}, reales))


@tag("unit")
class RequestTokenTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(sin_red())
        self.api = ApiFalsa()

    async def pedir_token(self, *respuestas, base_url=BASE_URL):
        self.api.responder(HOST, RUTA_TOKEN, *respuestas)
        async with httpx.AsyncClient(transport=self.api.transport) as cliente:
            return await adaptador(base_url).request_token(cliente)

    async def test_un_token_valido_devuelve_new_token_y_envia_el_formulario(self):
        """S3 Con 200 devuelve NewToken; el POST va a /oauth/token como formulario en minúsculas."""
        # Act
        token = await self.pedir_token(respuesta_muestra("syscom_token"))

        # Assert
        peticion = self.api.peticiones[0]
        self.assertEqual(
            (
                token,
                peticion.method,
                peticion.url.path,
                peticion.headers["content-type"],
                parse_qs(peticion.content.decode()),
            ),
            (
                NewToken(TOKEN_MUESTRA, 31536000),
                "POST",
                RUTA_TOKEN,
                "application/x-www-form-urlencoded",
                {
                    "grant_type": ["client_credentials"],
                    "client_id": [CLIENT_ID_FALSO],
                    "client_secret": [CENTINELA_SECRETO],
                },
            ),
        )

    async def test_credenciales_rechazadas_dan_error_de_autenticacion_sin_status(self):
        """S4 Un 400 o 401 del endpoint de tokens es ProviderAuthError sin status y sin el secreto."""
        for status in (400, 401):
            with self.subTest(status=status):
                # Act
                with self.assertRaises(ProviderAuthError) as error:
                    await self.pedir_token(respuesta_json({"error": "invalid_client"}, status=status))

                # Assert
                self.assertEqual(
                    (error.exception.status, str(error.exception)),
                    (None, f"[syscom] SYSCOM rechazó las credenciales (HTTP {status})."),
                )

    async def test_un_429_y_un_500_del_endpoint_de_tokens(self):
        """S5 Un 429 del endpoint de tokens es ProviderRateLimitError(retry_after); un 500, ProviderResponseError."""
        # Act
        with self.assertRaises(ProviderRateLimitError) as limite:
            await self.pedir_token(httpx.Response(429, headers={"Retry-After": "60"}))
        with self.assertRaises(ProviderResponseError) as caido:
            await self.pedir_token(httpx.Response(500))

        # Assert
        self.assertEqual(
            (limite.exception.retry_after, str(caido.exception)), (60, "[syscom] HTTP 500 del proveedor.")
        )

    async def test_una_respuesta_de_token_invalida_es_error_sin_filtrar_el_token(self):
        """S6 Una respuesta sin access_token/expires_in válidos, o que no es JSON, es ProviderResponseError."""
        casos = {
            "sin campos": respuesta_json({}),
            "sin expires_in": respuesta_json({"access_token": CENTINELA_TOKEN}),
            "expires_in en texto": respuesta_json({"access_token": CENTINELA_TOKEN, "expires_in": "31536000"}),
            "access_token vacío": respuesta_json({"access_token": "", "expires_in": 3600}),
            "lista": respuesta_json([CENTINELA_TOKEN]),
            "no es JSON": httpx.Response(200, text=f"access_token={CENTINELA_TOKEN}"),
        }
        for caso, respuesta in casos.items():
            with self.subTest(caso):
                # Act
                with self.assertRaises(ProviderResponseError) as error:
                    await self.pedir_token(respuesta)

                # Assert
                self.assertNotIn(CENTINELA_TOKEN, str(error.exception))

    async def test_con_base_url_terminada_en_barra_el_token_no_lleva_doble_barra(self):
        """S14 Con base_url terminada en "/" el POST va a /api/v1/oauth/token, sin doble barra."""
        # Act
        await self.pedir_token(respuesta_muestra("syscom_token"), base_url=BASE_URL + "/")

        # Assert
        self.assertEqual(self.api.peticiones[0].url.path, RUTA_TOKEN)


@tag("django_db")
class FetchTests(TestCase):
    def setUp(self):
        exigir_bd_de_pruebas()
        self.enterContext(sin_red())
        self.api = ApiFalsa()
        self.proveedor = Provider.objects.create(code="syscom", name="SYSCOM", base_url=BASE_URL)
        ahora = timezone.now()
        ProviderToken.objects.create(
            provider=self.proveedor,
            access_token="tok-guardado",
            expires_at=ahora + timedelta(days=30),
            obtained_at=ahora,
        )

    async def buscar(self, consulta, *respuestas):
        """Llama a fetch con el token ya guardado: no se pide otro."""
        self.api.responder(HOST, RUTA_PRODUCTOS, *respuestas)
        syscom = SyscomAdapter(self.proveedor, {"CLIENT_ID": CLIENT_ID_FALSO, "CLIENT_SECRET": CENTINELA_SECRETO})
        async with httpx.AsyncClient(transport=self.api.transport) as cliente:
            return await syscom.fetch(consulta, cliente)

    def parametros(self, indice=0):
        return dict(self.api.peticiones[indice].url.params)

    async def test_la_busqueda_envia_los_parametros_fijos_y_el_token_guardado(self):
        """S7 La búsqueda envía busqueda, limit, moneda=usd, iva=0 y Bearer, con el token guardado."""
        # Act
        items = await self.buscar(ProductQuery(term="camara ip"), respuesta_muestra("syscom_productos"))

        # Assert
        self.assertEqual(
            (
                self.api.rutas_pedidas(),
                self.parametros(),
                self.api.peticiones[0].headers["authorization"],
                [item["producto_id"] for item in items],
            ),
            (
                [PRODUCTOS],
                {"busqueda": "camara ip", "limit": "60", "moneda": "usd", "iva": "0"},
                "Bearer tok-guardado",
                ["900001", "900002", "900003"],
            ),
        )

    async def test_el_sku_busca_por_modelo_y_manda_sobre_el_termino(self):
        """S8 Con sku se pide ?modelo= (sin busqueda ni limit) aunque haya term; el objeto vuelve en una lista."""
        # Act
        items = await self.buscar(
            ProductQuery(term="camara", sku="CAM-IP-2MP-A1"), respuesta_muestra("syscom_modelo")
        )

        # Assert
        self.assertEqual(
            (self.parametros(), [item["modelo"] for item in items]),
            ({"modelo": "CAM-IP-2MP-A1", "moneda": "usd", "iva": "0"}, ["CAM-IP-2MP-A1"]),
        )

    async def test_una_busqueda_larga_se_recorta_a_10_palabras_y_120_caracteres(self):
        """S9 busqueda se recorta a 10 palabras y, después, a 120 caracteres."""
        # Arrange
        doce_palabras = " ".join(f"p{n}" for n in range(1, 13))
        tres_largas = " ".join(["a" * 50, "b" * 50, "c" * 50])

        # Act
        await self.buscar(ProductQuery(term=doce_palabras), respuesta_muestra("syscom_sin_resultados"))
        await self.buscar(ProductQuery(term=tres_largas), respuesta_muestra("syscom_sin_resultados"))

        # Assert
        self.assertEqual(
            (self.parametros(0)["busqueda"], self.parametros(1)["busqueda"]),
            (" ".join(f"p{n}" for n in range(1, 11)), tres_largas[:120]),
        )

    async def test_un_recorte_que_cae_en_un_espacio_no_lo_deja_al_final(self):
        """S9 Si el corte de 120 caracteres cae justo después de una palabra, no queda un espacio al final."""
        # Arrange: "a"*119 + " " + "b" → los 120 primeros acaban en espacio
        termino = "a" * 119 + " b"

        # Act
        await self.buscar(ProductQuery(term=termino), respuesta_muestra("syscom_sin_resultados"))

        # Assert
        self.assertEqual(self.parametros()["busqueda"], "a" * 119)

    async def test_un_limit_menor_que_10_pide_10_y_devuelve_solo_limit(self):
        """S10 Con limit < 10 se piden 10 (mínimo de SYSCOM) y se devuelven solo limit."""
        # Act
        items = await self.buscar(ProductQuery(term="camara", limit=2), respuesta_muestra("syscom_productos"))

        # Assert
        self.assertEqual((self.parametros()["limit"], len(items)), ("10", 2))

    async def test_una_respuesta_sin_la_forma_esperada_es_error(self):
        """S11 Una búsqueda sin lista de productos, o un modelo que no es un objeto, es ProviderResponseError."""
        sin_lista = "[syscom] Respuesta sin lista de productos."
        casos = [
            ("búsqueda sin productos", ProductQuery(term="camara"), respuesta_json({"cantidad": 0}), sin_lista),
            ("productos no es lista", ProductQuery(term="camara"), respuesta_json({"productos": {}}), sin_lista),
            ("búsqueda como lista", ProductQuery(term="camara"), respuesta_json([]), sin_lista),
            (
                "modelo como lista",
                ProductQuery(sku="X1"),
                respuesta_json([{"producto_id": "1"}]),
                "[syscom] Respuesta de producto inválida.",
            ),
        ]
        for caso, consulta, respuesta, mensaje in casos:
            with self.subTest(caso):
                # Act / Assert
                with self.assertRaisesMessage(ProviderResponseError, mensaje):
                    await self.buscar(consulta, respuesta)

    async def test_un_modelo_no_disponible_es_una_lista_vacia(self):
        """S12 Un 404 product_not_available en la búsqueda por modelo es "sin resultados": []."""
        # Act
        items = await self.buscar(
            ProductQuery(sku="NO-EXISTE"), respuesta_muestra("syscom_modelo_no_disponible", status=404)
        )

        # Assert
        self.assertEqual(items, [])

    async def test_cualquier_otro_404_es_error(self):
        """S13 Un 404 con otro cuerpo, sin JSON, o en la búsqueda por texto sigue siendo error."""
        casos = [
            ("modelo, otro error", ProductQuery(sku="X1"), respuesta_json({"error": "otro"}, status=404)),
            ("modelo, cuerpo HTML", ProductQuery(sku="X1"), httpx.Response(404, text="<html>Not Found</html>")),
            ("modelo, JSON no objeto", ProductQuery(sku="X1"), respuesta_json(["product_not_available"], 404)),
            (
                "búsqueda por texto",
                ProductQuery(term="camara"),
                respuesta_muestra("syscom_modelo_no_disponible", status=404),
            ),
        ]
        for caso, consulta, respuesta in casos:
            with self.subTest(caso):
                # Act / Assert
                with self.assertRaisesMessage(ProviderResponseError, "[syscom] HTTP 404 del proveedor."):
                    await self.buscar(consulta, respuesta)

    async def test_con_base_url_terminada_en_barra_la_ruta_no_lleva_doble_barra(self):
        """S14 Con base_url terminada en "/" la búsqueda va a /api/v1/productos, sin doble barra."""
        # Arrange
        self.proveedor.base_url = BASE_URL + "/"

        # Act
        await self.buscar(ProductQuery(term="camara"), respuesta_muestra("syscom_sin_resultados"))

        # Assert
        self.assertEqual(self.api.peticiones[0].url.path, RUTA_PRODUCTOS)


@tag("unit")
class ParseOfferTests(SimpleTestCase):
    def setUp(self):
        self.syscom = adaptador()

    def test_el_producto_de_muestra_se_convierte_en_la_oferta_esperada(self):
        """S15 Un producto con la estructura real da la oferta esperada: Decimal exacto, MPN = modelo, USD."""
        # Arrange
        item = producto_muestra(0)

        # Act
        oferta = self.syscom.parse_offer(item)

        # Assert
        self.assertEqual(
            (oferta, str(oferta.price), oferta.raw),
            (
                ProviderOffer(
                    provider_code="syscom",
                    external_id="900001",
                    name="Cámara bala IP 2 MP de muestra / Lente 2.8 mm / PoE",
                    price=Decimal("85.50"),
                    currency="USD",
                    brand="MARCA DE MUESTRA",
                    model="CAM-IP-2MP-A1",
                    mpn="CAM-IP-2MP-A1",
                    stock=25,
                ),
                "85.50",
                item,
            ),
        )

    def test_los_productos_invalidos_lanzan_excepciones_que_el_nucleo_descarta(self):
        """S16 Id, título o precio ausentes o inválidos, precio negativo o 0, o un elemento que no es objeto."""

        def sin(clave):
            item = producto_muestra()
            del item[clave]
            return item

        def con(**cambios):
            return {**producto_muestra(), **cambios}

        def con_precio(valor):
            item = producto_muestra()
            item["precios"]["precio_descuento"] = valor
            return item

        sin_precio_descuento = producto_muestra()
        del sin_precio_descuento["precios"]["precio_descuento"]
        casos = {
            "sin producto_id": sin("producto_id"),
            "producto_id None": con(producto_id=None),
            "producto_id bool": con(producto_id=True),
            "producto_id vacío": con(producto_id="  "),
            "sin título": sin("titulo"),
            "título vacío": con(titulo="   "),
            "título no es texto": con(titulo=123),
            "sin precios": sin("precios"),
            "precios None": con(precios=None),
            "sin precio_descuento": sin_precio_descuento,
            "precio no numérico": con_precio("N/A"),
            "precio negativo": con_precio("-1.00"),
            "precio 0": con_precio("0.00"),
            "precio float": con_precio(85.5),
            "no es un objeto": "producto",
        }
        for caso, item in casos.items():
            with self.subTest(caso):
                # Act / Assert
                with self.assertRaises(OFERTA_INVALIDA):
                    self.syscom.parse_offer(item)

    def test_existencias_marca_y_modelo_invalidos_quedan_en_none(self):
        """S17 Existencias no enteras o negativas, y marca o modelo que no son texto: None. 0 es válido.

        Los textos (título, marca y modelo) llegan sin espacios alrededor.
        """
        casos = [
            ("título con espacios", {"titulo": "  Cámara X  "}, "name", "Cámara X"),
            ("existencias negativas", {"total_existencia": -3}, "stock", None),
            ("existencias en texto", {"total_existencia": "25"}, "stock", None),
            ("existencias bool", {"total_existencia": True}, "stock", None),
            ("sin existencias", {"total_existencia": None}, "stock", None),
            ("existencias 0", {"total_existencia": 0}, "stock", 0),
            ("marca no es texto", {"marca": {"nombre": "X"}}, "brand", None),
            ("marca vacía", {"marca": "  "}, "brand", None),
            ("marca con espacios", {"marca": "  MARCA  "}, "brand", "MARCA"),
            ("modelo None", {"modelo": None}, "model", None),
            ("modelo vacío (mpn)", {"modelo": ""}, "mpn", None),
            ("modelo con espacios (mpn)", {"modelo": " AB-12 "}, "mpn", "AB-12"),
        ]
        for caso, cambios, campo, esperado in casos:
            with self.subTest(caso):
                # Act
                oferta = self.syscom.parse_offer({**producto_muestra(), **cambios})

                # Assert
                self.assertEqual(getattr(oferta, campo), esperado)


@tag("django_db")
class SyscomEnSearchAllTests(TestCase):
    """El adaptador real dentro de search_all, con el registro real y ApiFalsa."""

    def setUp(self):
        exigir_bd_de_pruebas()
        self.enterContext(sin_red())
        self.enterContext(
            credenciales_de_prueba("syscom", CLIENT_ID=CLIENT_ID_FALSO, CLIENT_SECRET=CENTINELA_SECRETO)
        )
        self.api = ApiFalsa()
        self.proveedor = Provider.objects.create(code="syscom", name="SYSCOM", base_url=BASE_URL)

    def token_responde(self, *respuestas):
        self.api.responder(HOST, RUTA_TOKEN, *(respuestas or (respuesta_muestra("syscom_token"),)))

    def productos_responden(self, *respuestas):
        self.api.responder(HOST, RUTA_PRODUCTOS, *respuestas)

    async def token_guardado(self, valor):
        ahora = timezone.now()
        await ProviderToken.objects.acreate(
            provider=self.proveedor, access_token=valor, expires_at=ahora + timedelta(days=300), obtained_at=ahora
        )

    async def buscar(self, consulta=ProductQuery(term="camara ip")):
        [resultado] = await search_all(consulta, transport=self.api.transport)
        return resultado

    async def test_una_busqueda_completa_devuelve_ofertas_y_cuenta_los_descartes(self):
        """S18 Pide el token, lo guarda, busca, convierte y descarta el producto con precio 0."""
        # Arrange
        datos = muestra("syscom_productos")
        con_precio_cero = copy.deepcopy(datos["productos"][0])
        con_precio_cero["producto_id"] = "900009"
        con_precio_cero["precios"]["precio_descuento"] = "0.00"
        datos["productos"].append(con_precio_cero)
        self.token_responde()
        self.productos_responden(respuesta_json(datos))

        # Act
        with self.assertLogs("apps.providers", "WARNING"):
            resultado = await self.buscar()

        # Assert
        guardado = await ProviderToken.objects.aget(provider=self.proveedor)
        self.assertEqual(
            (
                resultado.ok,
                [oferta.external_id for oferta in resultado.offers],
                [oferta.price for oferta in resultado.offers],
                resultado.discarded,
                guardado.access_token,
                self.api.rutas_pedidas(),
            ),
            (
                True,
                ["900001", "900002", "900003"],
                [Decimal("85.50"), Decimal("150.25"), Decimal("8.75")],
                1,
                TOKEN_MUESTRA,
                [TOKEN, PRODUCTOS],
            ),
        )

    async def test_el_token_se_reutiliza_en_la_segunda_busqueda(self):
        """S19 Dos búsquedas seguidas piden un solo token: la segunda reutiliza el guardado."""
        # Arrange
        self.token_responde()
        self.productos_responden(respuesta_muestra("syscom_productos"))

        # Act
        await self.buscar()
        await self.buscar()

        # Assert
        self.assertEqual(self.api.rutas_pedidas(), [TOKEN, PRODUCTOS, PRODUCTOS])

    async def test_un_401_en_productos_renueva_el_token_y_reintenta(self):
        """S20 Un 401 con el token guardado: el núcleo lo invalida, pide uno nuevo y reintenta una vez."""
        # Arrange
        await self.token_guardado("tok-revocado")
        self.token_responde()
        self.productos_responden(httpx.Response(401), respuesta_muestra("syscom_productos"))

        # Act
        resultado = await self.buscar()

        # Assert
        autorizaciones = [p.headers["authorization"] for p in self.api.peticiones if p.url.path == RUTA_PRODUCTOS]
        self.assertEqual(
            (resultado.ok, len(resultado.offers), autorizaciones, self.api.rutas_pedidas().count(TOKEN)),
            (True, 3, ["Bearer tok-revocado", f"Bearer {TOKEN_MUESTRA}"], 1),
        )

    async def test_un_403_no_se_reintenta(self):
        """S21 Un 403 (cuenta o scope ver-productos) es ProviderAuthError(403), sin reintento."""
        # Arrange
        self.token_responde()
        self.productos_responden(httpx.Response(403))

        # Act
        resultado = await self.buscar()

        # Assert
        self.assertEqual(
            (type(resultado.error), resultado.error.status, self.api.rutas_pedidas()),
            (ProviderAuthError, 403, [TOKEN, PRODUCTOS]),
        )

    async def test_un_429_devuelve_retry_after_sin_reintento(self):
        """S22 Un 429 en /productos es ProviderRateLimitError con su Retry-After, sin reintento."""
        # Arrange
        self.token_responde()
        self.productos_responden(httpx.Response(429, headers={"Retry-After": "30"}))

        # Act
        resultado = await self.buscar()

        # Assert
        self.assertEqual(
            (type(resultado.error), resultado.error.retry_after, self.api.rutas_pedidas()),
            (ProviderRateLimitError, 30, [TOKEN, PRODUCTOS]),
        )

    async def test_credenciales_rechazadas_piden_un_solo_token(self):
        """S23 Si SYSCOM rechaza las credenciales (401), no se reintenta: un solo POST y ningún GET."""
        # Arrange
        self.token_responde(respuesta_json({"error": "invalid_client"}, status=401))

        # Act
        resultado = await self.buscar()

        # Assert
        self.assertEqual(
            (type(resultado.error), resultado.error.status, self.api.rutas_pedidas()),
            (ProviderAuthError, None, [TOKEN]),
        )

    async def test_sin_credenciales_no_se_hace_ninguna_peticion(self):
        """S24 Sin PROVIDER_SYSCOM_* el núcleo devuelve ProviderConfigError sin llamar a SYSCOM."""
        # Act
        with credenciales_de_prueba("syscom"):
            resultado = await self.buscar()

        # Assert
        self.assertEqual(
            (str(resultado.error), self.api.peticiones),
            ("[syscom] Faltan credenciales: PROVIDER_SYSCOM_CLIENT_ID, PROVIDER_SYSCOM_CLIENT_SECRET", []),
        )

    async def test_un_sku_que_syscom_no_vende_da_ok_sin_ofertas(self):
        """S25 Un sku que SYSCOM no tiene (404 product_not_available) es un resultado ok con 0 ofertas."""
        # Arrange
        self.token_responde()
        self.productos_responden(respuesta_muestra("syscom_modelo_no_disponible", status=404))

        # Act
        resultado = await self.buscar(ProductQuery(sku="NO-EXISTE"))

        # Assert
        self.assertEqual((resultado.ok, resultado.offers, resultado.discarded), (True, (), 0))

    async def test_ni_el_token_ni_el_secreto_aparecen_en_logs_ni_mensajes(self):
        """S26 Con renovación, un descarte y un error final, ni el token ni el client_secret salen."""
        # Arrange
        await self.token_guardado("tok-revocado")
        self.token_responde(
            respuesta_json({"token_type": "Bearer", "expires_in": 31536000, "access_token": CENTINELA_TOKEN})
        )
        datos = muestra("syscom_productos")
        datos["productos"][0]["precios"]["precio_descuento"] = "abc"
        self.productos_responden(httpx.Response(401), respuesta_json(datos), httpx.Response(500))

        # Act
        with self.assertLogs(level="DEBUG") as registro:
            primero = await self.buscar()
            segundo = await self.buscar()

        # Assert
        texto = "\n".join(registro.output) + str(primero.error) + str(segundo.error)
        self.assertEqual(
            (CENTINELA_TOKEN in texto, CENTINELA_SECRETO in texto, primero.discarded, type(segundo.error)),
            (False, False, 1, ProviderResponseError),
        )


@tag("unit")
class RegistroTests(SimpleTestCase):
    def test_syscom_esta_en_el_registro_real(self):
        """S27 Al arrancar Django, syscom queda registrado con sus credenciales y uses_token."""
        # Assert
        self.assertEqual(
            (
                "syscom" in registered_codes(),
                get_adapter_class("syscom"),
                SyscomAdapter.required_credentials,
                SyscomAdapter.uses_token,
            ),
            (True, SyscomAdapter, ("CLIENT_ID", "CLIENT_SECRET"), True),
        )
