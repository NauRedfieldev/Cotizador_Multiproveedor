"""SDD M9 §8, pruebas E1–E28 (+ E29–E32: caché de búsquedas, docs/32 §11): scraper de la tienda de Exel del Norte.

Sin red y sin credenciales reales: ApiFalsa, sin_red() y credenciales_de_prueba() de
apps/providers/tests/soporte.py. Las muestras de muestras/ tienen la estructura real del sitio
(consultado el 2026-10-27, docs/31 §10) con valores ficticios.
"""
import copy
import json
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import httpx
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, tag

from apps.providers.adapters.base import NewToken
from apps.providers.contracts import ProductQuery, ProviderOffer
from apps.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from apps.providers.models import Provider, ProviderToken
from apps.providers.registry import get_adapter_class, registered_codes
from apps.providers.service import search_all
from apps.providers.tests.soporte import (
    ApiFalsa,
    credenciales_de_prueba,
    exigir_bd_de_pruebas,
    sin_red,
)
from apps.providers_web.base import WebScraperAdapter
from apps.providers_web.scrapers import exel
from apps.providers_web.scrapers.exel import ExelScraper

MUESTRAS = Path(__file__).parent / "muestras"
HOST = "www.exel.com.mx"
BASE_URL = "https://www.exel.com.mx/xlstore/"
RUTA_ACCESO = "/xlstore/Acceso"
RUTA_BUSQUEDA = "/xlstore/Productos/buscarJSON.aspx"
RUTA_PRECIOS = "/xlstore/Productos/buscarJSONprice.aspx"
ACCESO, BUSQUEDA, PRECIOS = (f"{HOST}{r}" for r in (RUTA_ACCESO, RUTA_BUSQUEDA, RUTA_PRECIOS))
USUARIO_FALSO = "usuario-de-prueba"
CENTINELA_CONTRASENA = "contrasena-centinela-qa-8d2e-no-debe-aparecer"
CENTINELA_SESION = "sesion-centinela-qa-5e1b-no-debe-aparecer"
SESION = f"ASP.NET_SessionId={CENTINELA_SESION}; TS01dc4fc6=balanceador-de-muestra"
HTML = "text/html; charset=utf-8"
OFERTA_INVALIDA = (ProviderResponseError, ValueError, TypeError, KeyError)
CAMPOS_PRIVADOS = (
    "nombre_agente", "correo_agente", "celular_agente", "horario_agente", "departamento_agente",
    "id_agente", "descripcion_localidad_cliente", "id_localidad_cliente", "url_booking",
)


def muestra(nombre):
    return json.loads((MUESTRAS / f"{nombre}.json").read_text(encoding="utf-8"), parse_float=Decimal)


def json_muestra(nombre):
    """Como Exel: JSON anunciado como text/html; los precios pasan por parse_json igual que en real."""
    return httpx.Response(200, content=(MUESTRAS / f"{nombre}.json").read_bytes(), headers={"content-type": HTML})


def json_de(datos, status=200):
    return httpx.Response(status, content=json.dumps(datos, default=str).encode(), headers={"content-type": HTML})


def pagina_de_acceso():
    return httpx.Response(200, content=(MUESTRAS / "exel_acceso.html").read_bytes(), headers=[
        ("content-type", HTML),
        ("set-cookie", f"ASP.NET_SessionId={CENTINELA_SESION}; path=/; HttpOnly"),
        ("set-cookie", "TS01dc4fc6=balanceador-de-muestra; Path=/"),
    ])


def entrada_correcta():
    return httpx.Response(302, headers={"location": "https://www.exel.com.mx/xlstore/", "content-type": HTML})


def login_rechazado():
    return httpx.Response(200, content=(MUESTRAS / "exel_login_rechazado.html").read_bytes(), headers={"content-type": HTML})


def scraper(base_url=BASE_URL):
    proveedor = Provider(code="exel", name="Exel del Norte", base_url=base_url)
    return ExelScraper(proveedor, {"USUARIO": USUARIO_FALSO, "CONTRASENA": CENTINELA_CONTRASENA})


def con_sesion(adaptador, sesion=SESION):
    """fetch sin BD: la sesión ya guardada la devuelve token() directamente."""
    adaptador.token = mock.AsyncMock(return_value=sesion)
    return adaptador


def item_muestra(indice=0):
    """Un producto tal como lo entrega fetch: campos permitidos + su precio de CCONOR."""
    producto = muestra("exel_busqueda")["Table"][indice]
    precios = {p["id_producto"]: p for p in muestra("exel_precios")}
    return copy.deepcopy(exel._crudo(producto, {k: {c: v[c] for c in exel.CAMPOS_PRECIO} for k, v in precios.items()}))


def api_de_busqueda(busqueda="exel_busqueda", precios="exel_precios"):
    api = ApiFalsa()
    api.responder(HOST, RUTA_BUSQUEDA, json_muestra(busqueda))
    api.responder(HOST, RUTA_PRECIOS, json_muestra(precios))
    return api


def parametros(peticion):
    return {k: v[0] for k, v in parse_qs(peticion.url.query.decode(), keep_blank_values=True).items()}


@tag("unit")
class RegistroTests(SimpleTestCase):
    def test_exel_esta_registrado_como_scraper_con_sesion(self):
        """E1 'exel' está en el registro de apps.providers, con sus credenciales y su sesión como token."""
        # Act
        clase = get_adapter_class("exel")

        # Assert
        self.assertIn("exel", registered_codes())
        self.assertIs(clase, ExelScraper)
        self.assertTrue(issubclass(clase, WebScraperAdapter))
        self.assertEqual((clase.required_credentials, clase.uses_token), (("USUARIO", "CONTRASENA"), True))


@tag("unit")
class RequestTokenTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(sin_red())
        self.api = ApiFalsa()

    async def iniciar_sesion(self, base_url=BASE_URL):
        async with httpx.AsyncClient(transport=self.api.transport) as cliente:
            return await scraper(base_url).request_token(cliente)

    async def test_envia_el_formulario_exacto_con_la_cookie_del_get(self):
        """E2 GET del formulario y POST con sus ocultos + usuario, contraseña y botón, con la cookie del GET."""
        # Arrange
        self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), entrada_correcta())

        # Act
        token = await self.iniciar_sesion()

        # Assert
        get, post = self.api.peticiones
        self.assertEqual((get.method, post.method), ("GET", "POST"))
        enviado = {k: v[0] for k, v in parse_qs(post.content.decode(), keep_blank_values=True).items()}
        self.assertEqual(enviado, {
            "__EVENTTARGET": "", "__EVENTARGUMENT": "", "__VIEWSTATE": "viewstate-de-muestra-no-real",
            "__VIEWSTATEGENERATOR": "A0B1C2D3", "__EVENTVALIDATION": "validacion-de-muestra-no-real",
            "ctl00$hdnIdCarrito_debugear": "", "ctl00$hdnMostrarMasterPopup": "0",
            "ctl00$hdnDirectorioRaiz": "https://www.exel.com.mx/xlstore/", "ctl00$hdnTokenCloudflare": "",
            "ctl00$ddlCategoria": "0", "ctl00$MainContent$hdnerror": "", "ctl00$MainContent$hdnToken": "",
            "ctl00$MainContent$txtUsuario": USUARIO_FALSO, "ctl00$MainContent$txtPassword": CENTINELA_CONTRASENA,
            "ctl00$MainContent$L_ButtonLogin": "Iniciar Sesión",
        })
        self.assertEqual(post.headers["Cookie"], SESION)
        self.assertEqual(post.headers["Referer"], "https://www.exel.com.mx/xlstore/Acceso")
        self.assertNotIn("Cookie", get.headers)
        self.assertEqual(token, NewToken(SESION, exel.VIDA_SESION))

    async def test_la_cookie_del_post_va_explicita_y_no_depende_del_cliente(self):
        """E2 El AsyncClient es compartido: la cookie del GET se envía en el POST aunque el cliente no la guarde."""
        # Arrange
        self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), entrada_correcta())

        async def olvidar_cookies(respuesta):
            cliente.cookies.clear()

        # Act
        async with httpx.AsyncClient(transport=self.api.transport, event_hooks={"response": [olvidar_cookies]}) as cliente:
            await scraper().request_token(cliente)

        # Assert
        self.assertEqual(self.api.peticiones[1].headers.get("Cookie"), SESION)

    async def test_la_sesion_incluye_las_cookies_nuevas_del_post(self):
        """E3 Si el POST fija o cambia cookies, el token las lleva (gana la última)."""
        # Arrange
        entrada = httpx.Response(302, headers=[
            ("location", "https://www.exel.com.mx/xlstore/"),
            ("set-cookie", "TS01dc4fc6=balanceador-nuevo; Path=/"),
            ("set-cookie", ".ASPXAUTH=auth-de-muestra; Path=/"),
        ])
        self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), entrada)

        # Act
        token = await self.iniciar_sesion()

        # Assert
        self.assertEqual(
            token.access_token,
            f"ASP.NET_SessionId={CENTINELA_SESION}; TS01dc4fc6=balanceador-nuevo; .ASPXAUTH=auth-de-muestra",
        )

    async def test_credenciales_rechazadas_dan_error_sin_status_ni_secretos(self):
        """E4 200 con «Usuario y/o contraseña incorrectos» → ProviderAuthError sin status (no se repite el login)."""
        # Arrange
        self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), login_rechazado())

        # Act
        with self.assertRaises(ProviderAuthError) as error:
            await self.iniciar_sesion()

        # Assert
        self.assertIsNone(error.exception.status)
        for secreto in (CENTINELA_CONTRASENA, USUARIO_FALSO, CENTINELA_SESION):
            self.assertNotIn(secreto, str(error.exception))

    async def test_una_redireccion_de_vuelta_al_acceso_es_un_rechazo(self):
        """E5 302 a /Acceso (no a la tienda) también es un inicio de sesión rechazado, sin status."""
        # Arrange
        vuelta = httpx.Response(302, headers={"location": "/xlstore/Acceso?error=1"})
        self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), vuelta)

        # Act / Assert
        with self.assertRaises(ProviderAuthError) as error:
            await self.iniciar_sesion()
        self.assertIsNone(error.exception.status)

    async def test_400_o_401_del_acceso_van_sin_status(self):
        """E6 Un 400/401 en el GET o el POST del acceso no provoca el reintento del núcleo."""
        for respuestas in (
            (httpx.Response(401),),
            (httpx.Response(400),),
            (pagina_de_acceso(), httpx.Response(401)),
            (pagina_de_acceso(), httpx.Response(400)),
        ):
            with self.subTest(status=[r.status_code for r in respuestas]):
                # Arrange
                self.api.responder(HOST, RUTA_ACCESO, *respuestas)

                # Act
                with self.assertRaises(ProviderAuthError) as error:
                    await self.iniciar_sesion()

                # Assert
                self.assertIsNone(error.exception.status)

    async def test_429_y_5xx_del_acceso_se_traducen_sin_reintentar(self):
        """E7 429 → ProviderRateLimitError con retry_after; 403 → ProviderAuthError(403); 500 → ProviderResponseError."""
        casos = (
            ((pagina_de_acceso(), httpx.Response(429, headers={"Retry-After": "30"})), ProviderRateLimitError),
            ((pagina_de_acceso(), httpx.Response(403)), ProviderAuthError),
            ((httpx.Response(500),), ProviderResponseError),
            ((pagina_de_acceso(), httpx.Response(500)), ProviderResponseError),
        )
        for respuestas, esperado in casos:
            with self.subTest(status=[r.status_code for r in respuestas]):
                # Arrange
                self.api.responder(HOST, RUTA_ACCESO, *respuestas)

                # Act / Assert
                with self.assertRaises(esperado) as error:
                    await self.iniciar_sesion()
                if esperado is ProviderRateLimitError:
                    self.assertEqual(error.exception.retry_after, 30)
                if esperado is ProviderAuthError:
                    self.assertEqual(error.exception.status, 403)

    async def test_sin_formulario_o_sin_cookies_es_un_cambio_del_sitio(self):
        """E8 Sin Form1, sin los campos de usuario/contraseña, sin cookies o con un 2xx raro → ProviderResponseError."""
        sin_cookies = httpx.Response(200, content=(MUESTRAS / "exel_acceso.html").read_bytes(), headers={"content-type": HTML})
        sin_contrasena = httpx.Response(
            200, content=b'<form id="Form1"><input name="ctl00$MainContent$txtUsuario"></form>',
            headers={"content-type": HTML},
        )
        casos = (
            (httpx.Response(200, content=b"<html><body>Mantenimiento</body></html>"),),
            (sin_contrasena,),
            (sin_cookies, entrada_correcta()),
            (pagina_de_acceso(), httpx.Response(204)),
        )
        for respuestas in casos:
            with self.subTest(respuestas=respuestas):
                # Arrange
                self.api.responder(HOST, RUTA_ACCESO, *respuestas)

                # Act / Assert
                with self.assertRaises(ProviderResponseError):
                    await self.iniciar_sesion()

    async def test_base_url_con_o_sin_barra_final(self):
        """E9 base_url con o sin barra final pide la misma ruta (sin //)."""
        for base_url in (BASE_URL, BASE_URL.rstrip("/")):
            with self.subTest(base_url=base_url):
                # Arrange
                self.api = ApiFalsa()
                self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), entrada_correcta())

                # Act
                await self.iniciar_sesion(base_url)

                # Assert
                self.assertEqual(self.api.rutas_pedidas(), [ACCESO, ACCESO])


@tag("unit")
class FetchTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(sin_red())
        cache.clear()  # la LocMemCache dura toda la suite: cada prueba empieza sin búsquedas guardadas

    async def buscar(self, api, query, adaptador=None):
        async with httpx.AsyncClient(transport=api.transport) as cliente:
            return await con_sesion(adaptador or scraper()).fetch(query, cliente)

    async def test_busca_y_pide_los_precios_con_la_sesion_guardada(self):
        """E10 buscarJSON con los parámetros fijos y luego buscarJSONprice por lote, con la cookie y cabeceras AJAX."""
        # Arrange
        api = api_de_busqueda()

        # Act
        items = await self.buscar(api, ProductQuery(term="camara ip"))

        # Assert
        busqueda, precios = api.peticiones
        self.assertEqual(api.rutas_pedidas(), [BUSQUEDA, PRECIOS])
        self.assertEqual(parametros(busqueda), {
            "buscar": "camara ip", "borrarExistencia": "0", "ordenarPor": "orden inicial",
            "id_categoria": "0", "id_subcategoria": "0", "id_marca": "", "pruebas": "false",
        })
        self.assertEqual(parametros(precios), {"listaproductos": "EXMCAMA900001|EXMCAMA900002|EXMSWIT900003"})
        for peticion in (busqueda, precios):
            self.assertEqual(peticion.headers["Cookie"], SESION)
            self.assertEqual(peticion.headers["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual([i["id_producto"] for i in items], ["EXMCAMA900001", "EXMCAMA900002", "EXMSWIT900003"])

    async def test_el_termino_se_normaliza_y_recorta_a_100_caracteres(self):
        """E11 Espacios repetidos se juntan y el término se corta al maxlength del buscador (100)."""
        # Arrange
        api = api_de_busqueda()
        termino = "camara   ip " + "x" * 200

        # Act
        await self.buscar(api, ProductQuery(term=termino))

        # Assert
        enviado = parametros(api.peticiones[0])["buscar"]
        self.assertEqual(enviado, ("camara ip " + "x" * 200)[:100])

    async def test_sku_manda_sobre_term_y_solo_vale_el_mismo_codigo(self):
        """E12 Con sku se busca el código y se descartan los «similares» de otro código (sin distinguir mayúsculas)."""
        # Arrange
        api = api_de_busqueda(busqueda="exel_modelo")

        # Act
        items = await self.buscar(api, ProductQuery(term="camara", sku="dm-ipc-900001"))

        # Assert
        self.assertEqual(parametros(api.peticiones[0])["buscar"], "dm-ipc-900001")
        self.assertEqual([i["codigo_proveedor"] for i in items], ["DM-IPC-900001"])
        self.assertEqual(parametros(api.peticiones[1]), {"listaproductos": "EXMCAMA900001"})

    async def test_limit_recorta_antes_de_pedir_los_precios(self):
        """E13 Solo se piden (y devuelven) los precios de los primeros `limit` productos."""
        # Arrange
        api = api_de_busqueda()

        # Act
        items = await self.buscar(api, ProductQuery(term="camara ip", limit=1))

        # Assert
        self.assertEqual(len(items), 1)
        self.assertEqual(parametros(api.peticiones[1]), {"listaproductos": "EXMCAMA900001"})

    async def test_sin_resultados_es_lista_vacia_sin_pedir_precios(self):
        """E14 Table vacía → [] y ninguna petición de precios; un sku sin coincidencia exacta, igual."""
        for busqueda, query in (
            ("exel_sin_resultados", ProductQuery(term="zzz")),
            ("exel_modelo", ProductQuery(sku="DM-NO-EXISTE")),
        ):
            with self.subTest(busqueda=busqueda):
                # Arrange
                api = api_de_busqueda(busqueda=busqueda)

                # Act
                items = await self.buscar(api, query)

                # Assert
                self.assertEqual((items, api.rutas_pedidas()), ([], [BUSQUEDA]))

    async def test_una_respuesta_con_otra_forma_es_un_cambio_del_sitio(self):
        """E15 Sin Table, Table que no es lista, o precios que no son lista → ProviderResponseError."""
        casos = (
            (json_de({"Tabla": []}), json_muestra("exel_precios")),
            (json_de({"Table": {}}), json_muestra("exel_precios")),
            (json_de([1, 2]), json_muestra("exel_precios")),
            (json_muestra("exel_busqueda"), json_de({"precios": []})),
        )
        for busqueda, precios in casos:
            with self.subTest(busqueda=busqueda.content[:30], precios=precios.content[:30]):
                # Arrange
                api = ApiFalsa()
                api.responder(HOST, RUTA_BUSQUEDA, busqueda)
                api.responder(HOST, RUTA_PRECIOS, precios)

                # Act / Assert
                with self.assertRaisesMessage(ProviderResponseError, "La estructura del sitio cambió"):
                    await self.buscar(api, ProductQuery(term="camara ip"))

    async def test_sesion_caducada_es_un_401_para_que_el_nucleo_renueve(self):
        """E16 Redirección, sin_usuario/sin_cliente/logout o la página de acceso → ProviderAuthError(401)."""
        respuestas = (
            httpx.Response(302, headers={"location": "/xlstore/Acceso"}),
            httpx.Response(200, content=b'"sin_usuario"'),
            httpx.Response(200, content=b"  SIN_CLIENTE "),
            httpx.Response(200, content=b"logout"),
            login_rechazado(),
            httpx.Response(200, content=(MUESTRAS / "exel_acceso.html").read_bytes()),  # sin «logout»
        )
        for ruta in (RUTA_BUSQUEDA, RUTA_PRECIOS):
            for respuesta in respuestas:
                with self.subTest(ruta=ruta, respuesta=respuesta.content[:20]):
                    # Arrange
                    api = api_de_busqueda()
                    api.responder(HOST, ruta, respuesta)

                    # Act
                    with self.assertRaises(ProviderAuthError) as error:
                        await self.buscar(api, ProductQuery(term="camara ip"))

                    # Assert
                    self.assertEqual(error.exception.status, 401)

    async def test_otros_errores_http_y_json_invalido_son_errores(self):
        """E17 404, 500 o un cuerpo que no es JSON (sin marca de sesión) → ProviderResponseError; 429 → límite."""
        casos = (
            (httpx.Response(404), ProviderResponseError),
            (httpx.Response(500), ProviderResponseError),
            (httpx.Response(200, content=b"<html>error</html>"), ProviderResponseError),
            (httpx.Response(429), ProviderRateLimitError),
        )
        for respuesta, esperado in casos:
            with self.subTest(status=respuesta.status_code, cuerpo=respuesta.content[:10]):
                # Arrange
                api = ApiFalsa()
                api.responder(HOST, RUTA_BUSQUEDA, respuesta)

                # Act / Assert
                with self.assertRaises(esperado):
                    await self.buscar(api, ProductQuery(term="camara ip"))

    async def test_el_crudo_no_lleva_datos_personales_y_si_su_precio(self):
        """E18 W9: raw solo con los campos permitidos (sin agente ni localidad) + el precio de CCONOR si lo hay."""
        # Arrange
        api = api_de_busqueda()

        # Act
        items = await self.buscar(api, ProductQuery(term="camara ip"))

        # Assert
        for item in items:
            self.assertLessEqual(set(item), set(exel.CAMPOS_PRODUCTO) | {"precios"})
            for campo in CAMPOS_PRIVADOS:
                self.assertNotIn(campo, item)
        self.assertEqual(items[0]["precios"], {
            "precio": Decimal("1000.00"), "precio_lista": Decimal("1818.1818"),
            "precio_normal": Decimal("1818.1818"), "origen": "N", "vigencia": "2026-01-01T00:00:00",
        })
        self.assertNotIn("precios", items[2])  # Exel no devolvió su precio: parse_offer lo descartará

    async def test_productos_sin_id_valido_no_se_piden_ni_reciben_precio(self):
        """E18 Un id que no es texto seguro no va a buscarJSONprice; un elemento que no es objeto pasa tal cual."""
        # Arrange
        busqueda = muestra("exel_busqueda")
        busqueda["Table"][1]["id_producto"] = "EXM 9|00002"
        busqueda["Table"].append("no-es-un-producto")
        api = ApiFalsa()
        api.responder(HOST, RUTA_BUSQUEDA, json_de(busqueda))
        api.responder(HOST, RUTA_PRECIOS, json_muestra("exel_precios"))

        # Act
        items = await self.buscar(api, ProductQuery(term="camara ip"))

        # Assert
        self.assertEqual(parametros(api.peticiones[1]), {"listaproductos": "EXMCAMA900001|EXMSWIT900003"})
        self.assertNotIn("precios", items[1])
        self.assertEqual(items[3], "no-es-un-producto")

    async def test_sin_ningun_id_valido_no_se_piden_precios(self):
        """E18 Si ningún producto tiene id válido, no hay petición de precios y ninguno lleva precio."""
        # Arrange
        busqueda = muestra("exel_busqueda")
        for producto in busqueda["Table"]:
            producto["id_producto"] = 900001
        api = ApiFalsa()
        api.responder(HOST, RUTA_BUSQUEDA, json_de(busqueda))

        # Act
        items = await self.buscar(api, ProductQuery(term="camara ip"))

        # Assert
        self.assertEqual(api.rutas_pedidas(), [BUSQUEDA])
        self.assertTrue(all("precios" not in i for i in items))

    async def test_una_busqueda_repetida_sale_de_la_cache_sin_sesion_ni_peticiones(self):
        """E29 La misma búsqueda (con los espacios ya normalizados) no pide sesión ni hace peticiones; [] también se guarda."""
        for busqueda, primera, repetida in (
            ("exel_busqueda", ProductQuery(term="camara ip"), ProductQuery(term="  camara   ip ")),
            ("exel_sin_resultados", ProductQuery(term="zzz"), ProductQuery(term="zzz")),
        ):
            with self.subTest(busqueda=busqueda):
                # Arrange
                api = api_de_busqueda(busqueda=busqueda)
                adaptador = con_sesion(scraper())
                esperado = await self.buscar(api, primera, adaptador)
                api.peticiones.clear()

                # Act
                items = await self.buscar(api, repetida, adaptador)

                # Assert
                self.assertEqual(items, esperado)
                self.assertEqual(api.peticiones, [])
                self.assertEqual(adaptador.token.await_count, 0)  # buscar() le pone un token nuevo en cada llamada

    async def test_otro_termino_sku_o_limit_no_aprovechan_la_cache(self):
        """E30 La clave cambia con el texto enviado, el sku (que filtra) y limit; con sku, term no cuenta."""
        # Arrange
        await self.buscar(api_de_busqueda(), ProductQuery(term="camara ip"))
        for query in (
            ProductQuery(term="camara"),
            ProductQuery(term="camara ip", sku="camara ip"),
            ProductQuery(term="camara ip", sku="DM-IPC-900001"),
            ProductQuery(term="camara ip", limit=1),
        ):
            with self.subTest(query=query):
                api = api_de_busqueda(busqueda="exel_modelo" if query.sku else "exel_busqueda")

                # Act
                await self.buscar(api, query)

                # Assert
                self.assertEqual(api.rutas_pedidas()[0], BUSQUEDA)
        self.assertEqual(
            exel._clave_cache(ProductQuery(term="camara", sku="DM-IPC-900001")),
            exel._clave_cache(ProductQuery(term="otra cosa", sku="DM-IPC-900001")),
        )

    async def test_un_error_no_se_guarda_en_la_cache(self):
        """E31 Tras un error (HTTP, sesión caducada o estructura cambiada) la misma búsqueda vuelve a pedirse."""
        for ruta, fallo in (
            (RUTA_BUSQUEDA, httpx.Response(500)),
            (RUTA_BUSQUEDA, httpx.Response(302, headers={"location": "/xlstore/Acceso"})),
            (RUTA_PRECIOS, json_de({"precios": []})),
        ):
            with self.subTest(ruta=ruta, status=fallo.status_code):
                # Arrange
                api = api_de_busqueda()
                api.responder(HOST, ruta, fallo, json_muestra("exel_busqueda" if ruta == RUTA_BUSQUEDA else "exel_precios"))
                with self.assertRaises((ProviderResponseError, ProviderAuthError)):
                    await self.buscar(api, ProductQuery(term="camara ip"))
                api.peticiones.clear()

                # Act
                items = await self.buscar(api, ProductQuery(term="camara ip"))

                # Assert
                self.assertEqual(api.rutas_pedidas(), [BUSQUEDA, PRECIOS])
                self.assertEqual(len(items), 3)
                cache.clear()

    async def test_se_guarda_cinco_minutos_con_una_clave_sin_la_sesion(self):
        """E32 aset recibe la lista que devuelve fetch, SEGUNDOS_CACHE (300) y una clave que no lleva la cookie."""
        # Arrange
        api = api_de_busqueda()

        # Act
        with mock.patch.object(exel.cache, "aset", wraps=cache.aset) as guardar:
            items = await self.buscar(api, ProductQuery(term="camara ip"))

        # Assert
        (clave, guardado, segundos), _ = guardar.call_args
        self.assertEqual((guardado, segundos, exel.SEGUNDOS_CACHE), (items, 300, 300))
        self.assertRegex(clave, r"^exel:busqueda:[0-9a-f]{64}$")
        self.assertNotIn(CENTINELA_SESION, clave)


@tag("unit")
class ParseOfferTests(SimpleTestCase):
    def test_la_muestra_produce_exactamente_la_oferta_esperada(self):
        """E19 Precio de CCONOR en Decimal exacto, MXN, código como modelo y MPN, existencias nacionales, foto y ficha."""
        # Arrange
        item = item_muestra()

        # Act
        oferta = scraper().parse_offer(item)

        # Assert
        self.assertEqual(oferta, ProviderOffer(
            provider_code="exel",
            external_id="EXMCAMA900001",
            name="Cámara IP DE MUESTRA Domo 4 MP Lente 2.8 mm IR 30 m Micrófono Integrado",
            price=Decimal("1000.00"),
            currency="MXN",
            brand="MARCA DE MUESTRA",
            model="DM-IPC-900001",
            mpn="DM-IPC-900001",
            stock=25,
            image_url="https://contenidos.exel.com.mx/imgProducto/EXMCAMA900001.png",
            product_url="https://www.exel.com.mx/xlstore/Productos/Detalle/EXMCAMA900001",
        ))
        self.assertEqual(str(oferta.price), "1000.00")
        self.assertIs(oferta.raw, item)

    def test_productos_invalidos_lanzan_una_excepcion_que_el_nucleo_descarta(self):
        """E20 Sin descripción, sin precio de CCONOR, precio 0/negativo/no numérico, id inválido → se descarta."""
        def con(**cambios):
            item = item_muestra()
            for ruta, valor in cambios.items():
                destino, clave = (item["precios"], "precio") if ruta == "precio" else (item, ruta)
                if valor is KeyError:
                    del destino[clave]
                else:
                    destino[clave] = valor
            return item

        casos = {
            "sin descripcion": con(descripcion=KeyError),
            "descripcion vacía": con(descripcion="   "),
            "descripcion no texto": con(descripcion=900001),
            "sin precios": con(precios=KeyError),
            "sin precio": con(precio=KeyError),
            "precio 0": con(precio=Decimal("0.00")),
            "precio negativo": con(precio=Decimal("-1")),
            "precio no numérico": con(precio="abc"),
            "precio float": con(precio=1000.0),
            "id numérico": con(id_producto=900001),
            "id vacío": con(id_producto="  "),
            "sin id": con(id_producto=KeyError),
        }
        for nombre, item in casos.items():
            with self.subTest(nombre):
                # Act / Assert
                with self.assertRaises(OFERTA_INVALIDA):
                    scraper().parse_offer(item)

    def test_marca_codigo_existencias_y_foto_invalidos_quedan_en_none(self):
        """E21 Marca, código, existencias, foto o ficha inválidos dan None y la oferta se conserva."""
        casos = {
            "Marca": (None, "brand"), "codigo_proveedor": ("   ", "model"),
            "ExistenciaNacional": (True, "stock"), "tieneFoto": (0, "image_url"),
        }
        for campo, (valor, atributo) in casos.items():
            with self.subTest(campo):
                # Arrange
                item = item_muestra()
                item[campo] = valor

                # Act
                oferta = scraper().parse_offer(item)

                # Assert
                self.assertIsNone(getattr(oferta, atributo))
        for valor in (-1, "25", None):
            with self.subTest(existencias=valor):
                item = item_muestra()
                item["ExistenciaNacional"] = valor
                self.assertIsNone(scraper().parse_offer(item).stock)
        for valor in (True, "1", None):
            with self.subTest(tiene_foto=valor):
                item = item_muestra()
                item["tieneFoto"] = valor
                self.assertIsNone(scraper().parse_offer(item).image_url)

    def test_un_id_con_caracteres_raros_no_genera_foto_ni_ficha(self):
        """E21 Las URL solo se arman con ids seguros ([A-Za-z0-9._-]); el id se conserva sin espacios alrededor."""
        # Arrange
        item = item_muestra()
        item["id_producto"] = " EXM/../900001 "

        # Act
        oferta = scraper().parse_offer(item)

        # Assert
        self.assertEqual((oferta.external_id, oferta.image_url, oferta.product_url), ("EXM/../900001", None, None))
        self.assertEqual(scraper().parse_offer(item_muestra()).product_url[:8], "https://")


@tag("django_db")
class ExelEnSearchAllTests(TestCase):
    def setUp(self):
        exigir_bd_de_pruebas()
        self.enterContext(sin_red())
        cache.clear()
        self.enterContext(credenciales_de_prueba("exel", USUARIO=USUARIO_FALSO, CONTRASENA=CENTINELA_CONTRASENA))
        self.api = api_de_busqueda()
        self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), entrada_correcta())
        self.proveedor = Provider.objects.create(
            code="exel", name="Exel del Norte", base_url=BASE_URL, timeout_ms=10000
        )

    async def buscar(self, query=None):
        [resultado] = await search_all(query or ProductQuery(term="camara ip"), transport=self.api.transport)
        return resultado

    async def test_una_busqueda_completa_devuelve_ofertas_y_cuenta_los_descartes(self):
        """E22 Inicia sesión, la guarda, busca, pide precios, convierte y descarta el producto sin precio."""
        # Act
        resultado = await self.buscar()

        # Assert
        self.assertTrue(resultado.ok, resultado.error)
        self.assertEqual([o.external_id for o in resultado.offers], ["EXMCAMA900001", "EXMCAMA900002"])
        self.assertEqual([o.price for o in resultado.offers], [Decimal("1000.00"), Decimal("110.00")])
        self.assertEqual({o.currency for o in resultado.offers}, {"MXN"})
        self.assertEqual(resultado.discarded, 1)
        self.assertEqual(self.api.rutas_pedidas(), [ACCESO, ACCESO, BUSQUEDA, PRECIOS])
        token = await ProviderToken.objects.aget(provider=self.proveedor)
        self.assertEqual(token.access_token, SESION)

    async def test_la_sesion_se_reutiliza_entre_busquedas(self):
        """E23 Dos búsquedas distintas (la caché no sirve): un solo inicio de sesión (GET + POST)."""
        # Act
        await self.buscar()
        await self.buscar(ProductQuery(term="switch"))

        # Assert
        self.assertEqual(self.api.rutas_pedidas().count(ACCESO), 2)
        self.assertEqual(self.api.rutas_pedidas().count(BUSQUEDA), 2)

    async def test_una_sesion_caducada_se_renueva_una_vez(self):
        """E24 La búsqueda redirige a /Acceso → el núcleo invalida, vuelve a entrar y reintenta una vez."""
        # Arrange
        await self.buscar()
        self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), entrada_correcta())
        self.api.responder(
            HOST, RUTA_BUSQUEDA,
            httpx.Response(302, headers={"location": "/xlstore/Acceso"}), json_muestra("exel_busqueda"),
        )
        self.api.peticiones.clear()

        # Act
        resultado = await self.buscar(ProductQuery(term="switch"))

        # Assert
        self.assertTrue(resultado.ok, resultado.error)
        self.assertEqual(self.api.rutas_pedidas(), [BUSQUEDA, ACCESO, ACCESO, BUSQUEDA, PRECIOS])

    async def test_si_tras_renovar_sigue_caducada_no_hay_un_tercer_intento(self):
        """E24 Si la sesión nueva tampoco sirve, error 401 sin más logins (uno inicial + uno de renovación)."""
        # Arrange
        self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), entrada_correcta(), pagina_de_acceso(), entrada_correcta())
        self.api.responder(HOST, RUTA_BUSQUEDA, httpx.Response(302, headers={"location": "/xlstore/Acceso"}))

        # Act
        resultado = await self.buscar()

        # Assert
        self.assertIsInstance(resultado.error, ProviderAuthError)
        self.assertEqual(self.api.rutas_pedidas().count(ACCESO), 4)

    async def test_credenciales_rechazadas_hacen_un_solo_intento(self):
        """E25 Login rechazado → ProviderAuthError y un solo POST: no se repite para no bloquear la cuenta."""
        # Arrange
        self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), login_rechazado())

        # Act
        resultado = await self.buscar()

        # Assert
        self.assertIsInstance(resultado.error, ProviderAuthError)
        self.assertEqual([p.method for p in self.api.peticiones], ["GET", "POST"])
        self.assertFalse(await ProviderToken.objects.filter(provider=self.proveedor).aexists())

    async def test_sin_credenciales_no_hay_ninguna_peticion(self):
        """E26 Sin PROVIDER_EXEL_* → ProviderConfigError con los nombres de las variables y 0 peticiones."""
        # Arrange
        with credenciales_de_prueba("exel"):
            # Act
            resultado = await self.buscar()

        # Assert
        self.assertIsInstance(resultado.error, ProviderConfigError)
        self.assertIn("PROVIDER_EXEL_USUARIO", str(resultado.error))
        self.assertEqual(self.api.peticiones, [])

    async def test_ni_la_contrasena_ni_la_sesion_aparecen_en_logs_ni_mensajes(self):
        """E27 Centinelas de contraseña y cookie ausentes de los logs y del error, en éxito y en rechazo."""
        # Act
        with self.assertLogs("apps.providers", level="DEBUG") as logs:
            exito = await self.buscar()
            await ProviderToken.objects.filter(provider=self.proveedor).adelete()
            self.api.responder(HOST, RUTA_ACCESO, pagina_de_acceso(), login_rechazado())
            rechazo = await self.buscar(ProductQuery(term="switch"))

        # Assert
        textos = "\n".join(logs.output) + str(exito.error) + str(rechazo.error) + repr(rechazo)
        for secreto in (CENTINELA_CONTRASENA, CENTINELA_SESION):
            self.assertNotIn(secreto, textos)

    async def test_sin_resultados_es_ok_con_cero_ofertas(self):
        """E28 Una búsqueda sin resultados sale ok, con 0 ofertas y sin pedir precios."""
        # Arrange
        self.api.responder(HOST, RUTA_BUSQUEDA, json_muestra("exel_sin_resultados"))

        # Act
        resultado = await self.buscar(ProductQuery(term="zzz"))

        # Assert
        self.assertEqual((resultado.ok, resultado.offers, resultado.discarded), (True, (), 0))
        self.assertNotIn(PRECIOS, self.api.rutas_pedidas())
