"""Contrato HTTP de cotizar_productos (M7, docs/26 y docs/28), sin red ni credenciales reales.

CotizarProductosTests (del equipo, M7b): página en blanco, validaciones y 503 sin
proveedores activos, todo antes de cualquier HTTP. VistaCotizacionTests (V1–V16): modo
degradado con adaptadores de prueba cuyo fetch no hace HTTP.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

import httpx
from asgiref.sync import sync_to_async

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from apps.catalog.constants import EstadoMatch, MetodoMatch
from apps.catalog.matching import aemparejar_con_fallback
from apps.catalog.models import Category, ProductoCanonico
from apps.providers.models import Provider
from apps.providers.tests.soporte import (
    adaptadores_registrados,
    crear_adaptador,
    crear_proveedor,
    exigir_bd_de_pruebas,
    producto,
    sin_red,
)
from apps.quotes.views import (CLAVE_TIPO_CAMBIO, CLAVE_ULTIMA_BUSQUEDA, LIMITE_COTIZACIONES, clave_limite, fecha_exacta,
                               hace_texto)

PLANTILLA = "comparador.html"


def crear_asesor(username="asesor", password=None, tipo_cambio=True, **campos):
    """Usuario sintético. Sin password no se calcula ningún hash (force_login no lo necesita).

    Lienzo fila 13: por defecto puede cambiar el tipo de cambio (como un Asesor); tipo_cambio=False = Personal.
    """
    from django.contrib.auth.models import Permission
    usuario = get_user_model().objects.create_user(username, password=password, **campos)
    codigos = ["view_commercial_costs"] + (["change_commercial_fx"] if tipo_cambio else [])
    usuario.user_permissions.add(*Permission.objects.filter(content_type__app_label="quotes", codename__in=codigos))
    return usuario

CLAVES = (
    "ofertas",
    "proveedores_caidos",
    "proveedores_caidos_nombres",
    "tipo_cambio",
    "categorias",
    "error",
)


@tag("django_db")
class CotizarProductosTests(TestCase):
    def setUp(self):
        exigir_bd_de_pruebas()
        self.enterContext(sin_red())
        cache.clear()  # el rate limit vive en la LocMemCache, que dura toda la suite
        self.asesor = crear_asesor()
        self.client.force_login(self.asesor)
        self.url = reverse("cotizar_productos")
        Category.objects.create(name="Alfas", is_active=True)
        Category.objects.create(name="Zetas", is_active=True)
        Category.objects.create(name="Oculta", is_active=False)

    def assert_claves(self, respuesta):
        self.assertTemplateUsed(respuesta, "comparador.html")
        for clave in CLAVES:
            self.assertIn(clave, respuesta.context)

    def test_get_sin_parametros_responde_200_sin_ofertas_ni_error(self):
        """Página en blanco: 200, ofertas vacías, sin error y solo categorías activas."""
        respuesta = self.client.get(self.url)

        self.assertEqual(respuesta.status_code, 200)
        self.assert_claves(respuesta)
        self.assertEqual(respuesta.context["ofertas"], [])
        self.assertIsNone(respuesta.context["error"])
        self.assertIsNone(respuesta.context["tipo_cambio"])
        self.assertEqual(respuesta.context["proveedores_caidos"], [])
        self.assertEqual(respuesta.context["proveedores_caidos_nombres"], [])
        self.assertEqual(
            [c.name for c in respuesta.context["categorias"]],
            ["Alfas", "Zetas"],
        )

    def test_parametros_invalidos_responden_400_con_error(self):
        """Coma decimal y categoría inexistente son 400, con ofertas vacías."""
        casos = (
            (
                {"q": "switch", "tipo_cambio": "17,50"},
                "decimal positivo con punto",
            ),
            (
                {"q": "switch", "categoria": "999999"},
                "categoría seleccionada no es válida",
            ),
        )
        for params, fragmento in casos:
            with self.subTest(params=params):
                respuesta = self.client.get(self.url, params)

                self.assertEqual(respuesta.status_code, 400)
                self.assert_claves(respuesta)
                self.assertEqual(respuesta.context["ofertas"], [])
                self.assertIn(fragmento, respuesta.context["error"])
                self.assertContains(respuesta, respuesta.context["error"], status_code=400)
                self.assertNotContains(
                    respuesta, "No se encontraron productos.", status_code=400,
                )

    def test_sin_proveedores_activos_responde_503(self):
        """Un proveedor inactivo no cuenta: no hay a quién consultar."""
        Provider.objects.create(
            code="inactivo", name="Inactivo", base_url="https://inactivo.test", active=False,
        )

        respuesta = self.client.get(self.url, {"q": "switch"})

        self.assertEqual(respuesta.status_code, 503)
        self.assert_claves(respuesta)
        self.assertEqual(respuesta.context["ofertas"], [])
        self.assertEqual(
            respuesta.context["error"],
            "No hay proveedores activos para consultar.",
        )
        self.assertEqual(respuesta.context["proveedores_caidos"], [])
        self.assertContains(respuesta, respuesta.context["error"], status_code=503)

    def test_la_plantilla_recibe_las_seis_claves_del_contexto(self):
        """200, 400 y 503 renderizan comparador.html con el contrato completo."""
        respuestas = (
            self.client.get(self.url),
            self.client.get(self.url, {"sku": "SW-1", "tipo_cambio": "17,50"}),
            self.client.get(self.url, {"sku": "SW-1"}),
        )
        self.assertEqual([r.status_code for r in respuestas], [200, 400, 503])
        for respuesta in respuestas:
            with self.subTest(status=respuesta.status_code):
                self.assert_claves(respuesta)
                self.assertEqual(len(CLAVES), 6)


def mxn(id_, precio, **campos):
    return producto(id_, precio=precio, moneda="MXN", **campos)


def usd(id_, precio, **campos):
    return producto(id_, precio=precio, moneda="USD", **campos)


def adaptador(code, *items, error=None, llamadas=None):
    """Adaptador sin HTTP: devuelve items o lanza error. Anota cada consulta en llamadas."""

    async def fetch(self, query, client):
        if llamadas is not None:
            llamadas.append(code)
        if error is not None:
            raise error
        return list(items)

    return crear_adaptador(code, fetch=fetch)


@tag("django_db")
@override_settings(OFERTAS_POR_PROVEEDOR=3)
class VistaCotizacionTests(TestCase):
    """V1–V16: la vista async en modo degradado (docs/26 y docs/28), sin red."""

    def setUp(self):
        cache.clear()
        crear_proveedor("alfa")
        crear_proveedor("beta")
        self.asesor = crear_asesor()
        self.async_client.force_login(self.asesor)

    async def buscar(self, adaptadores, **parametros):
        parametros.setdefault("q", "camara ip")
        with adaptadores_registrados(*adaptadores), sin_red():
            return await self.async_client.get(reverse("cotizar_productos"), parametros)

    def assertSinTraza(self, respuesta):
        self.assertTemplateUsed(respuesta, PLANTILLA)
        self.assertNotIn(b"Traceback", respuesta.content)

    async def test_v1_un_proveedor_caido_da_200_con_aviso(self):
        respuesta = await self.buscar([
            adaptador("alfa", mxn("1", "100.00", modelo="QA-MODELO")),
            adaptador("beta", error=TimeoutError()),
        ])
        self.assertEqual(respuesta.status_code, 200)
        self.assertSinTraza(respuesta)
        self.assertEqual(respuesta.context["proveedores_caidos"], ["beta"])
        self.assertEqual([o.oferta.provider_code for o in respuesta.context["ofertas"]], ["alfa"])
        self.assertIsNone(respuesta.context["error"])
        self.assertContains(respuesta, "Buscar en cotizador")
        self.assertContains(respuesta, '/cotizador/?sku=QA-MODELO')
        self.assertNotContains(respuesta, "Agregar a cotización")

    async def test_v2_ambos_caidos_da_503_sin_traza(self):
        respuesta = await self.buscar([
            adaptador("alfa", error=TimeoutError()),
            adaptador("beta", error=TimeoutError()),
        ])
        self.assertEqual(respuesta.status_code, 503)
        self.assertSinTraza(respuesta)
        self.assertEqual(respuesta.context["proveedores_caidos"], ["alfa", "beta"])
        self.assertEqual(respuesta.context["ofertas"], [])
        self.assertIsNotNone(respuesta.context["error"])

    async def test_v3_ambos_vivos_ordenados_por_precio_comparable(self):
        respuesta = await self.buscar([
            adaptador("alfa", mxn("1", "300.00"), mxn("2", "100.00")),
            adaptador("beta", mxn("1", "200.00")),
        ])
        self.assertEqual(respuesta.status_code, 200)
        ofertas = respuesta.context["ofertas"]
        self.assertEqual(
            [(o.oferta.provider_code, o.precio_comparable) for o in ofertas],
            [("alfa", Decimal("100.00")), ("beta", Decimal("200.00")), ("alfa", Decimal("300.00"))],
        )
        self.assertEqual([o.es_mejor_precio for o in ofertas], [True, True, False])
        self.assertEqual(respuesta.context["proveedores_caidos"], [])

    async def test_v4_usd_exige_tipo_de_cambio_y_convierte_exacto(self):
        adaptadores = [adaptador("alfa", usd("1", "10.25")), adaptador("beta", mxn("1", "200.00"))]
        sin_tipo = await self.buscar(adaptadores)
        self.assertEqual(sin_tipo.status_code, 400)
        self.assertSinTraza(sin_tipo)
        self.assertIn("tipo de cambio", sin_tipo.context["error"])

        con_tipo = await self.buscar(adaptadores, tipo_cambio=" 17.50 ")
        self.assertEqual(con_tipo.status_code, 200)
        tipo = con_tipo.context["tipo_cambio"]
        self.assertEqual((tipo.valor, tipo.origen), (Decimal("17.50"), "manual"))
        self.assertEqual(
            [(o.oferta.provider_code, o.precio_comparable) for o in con_tipo.context["ofertas"]],
            [("alfa", Decimal("179.3750")), ("beta", Decimal("200.00"))],
        )

    async def test_v5_entrada_invalida_da_400_sin_consultar_proveedores(self):
        inactiva = await Category.objects.acreate(name="Descontinuada", is_active=False)
        invalidas = [
            {"tipo_cambio": "17,50"},
            {"tipo_cambio": "0"},
            {"categoria": "abc"},
            {"categoria": "0"},
            {"categoria": "-3"},
            {"categoria": "99999999999"},
            {"categoria": "999"},  # no existe
            {"categoria": str(inactiva.pk)},  # no está en el formulario
        ]
        for parametros in invalidas:
            with self.subTest(parametros=parametros):
                cache.clear()
                llamadas = []
                respuesta = await self.buscar(
                    [adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)], **parametros
                )
                self.assertEqual(respuesta.status_code, 400)
                self.assertSinTraza(respuesta)
                self.assertTrue(respuesta.context["error"])
                self.assertEqual(llamadas, [])

    async def test_v6_ningun_proveedor_activo_da_503(self):
        await Provider.objects.aupdate(active=False)
        respuesta = await self.buscar([adaptador("alfa", mxn("1", "1.00"))])
        self.assertEqual(respuesta.status_code, 503)
        self.assertSinTraza(respuesta)
        self.assertEqual(respuesta.context["proveedores_caidos"], [])

    async def test_v7_rate_limit_da_429_sin_consultar_proveedores(self):
        llamadas = []
        adaptadores = [adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)]
        for _ in range(LIMITE_COTIZACIONES):
            self.assertEqual((await self.buscar(adaptadores)).status_code, 200)
        respuesta = await self.buscar(adaptadores)
        self.assertEqual(respuesta.status_code, 429)
        self.assertSinTraza(respuesta)
        self.assertEqual(len(llamadas), LIMITE_COTIZACIONES)

    async def test_v7c_el_limite_es_por_usuario_aunque_compartan_ip(self):
        adaptadores = [adaptador("alfa", mxn("1", "1.00"))]
        for _ in range(LIMITE_COTIZACIONES):
            self.assertEqual((await self.buscar(adaptadores)).status_code, 200)
        self.assertEqual((await self.buscar(adaptadores)).status_code, 429)
        await self.async_client.aforce_login(await get_user_model().objects.acreate_user("asesor_dos"))    # misma IP (127.0.0.1)
        self.assertEqual((await self.buscar(adaptadores)).status_code, 200)
        self.assertEqual(LIMITE_COTIZACIONES, 20)

    async def test_v7b_clave_caducada_entre_aadd_y_aincr_cuenta_como_primera(self):
        with mock.patch.object(cache, "aincr", side_effect=ValueError("Key not found")):
            respuesta = await self.buscar([adaptador("alfa", mxn("1", "1.00"))])
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(await cache.aget(clave_limite(self.asesor)), 1)

    async def test_v8_excepcion_inesperada_de_un_adaptador_queda_degradada(self):
        with self.assertLogs("apps.providers", "ERROR"):
            respuesta = await self.buscar([
                adaptador("alfa", mxn("1", "1.00")),
                adaptador("beta", error=RuntimeError("fallo interno")),
            ])
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["proveedores_caidos"], ["beta"])
        self.assertNotIn(b"fallo interno", respuesta.content)

    async def test_v8b_fallo_del_proceso_en_search_all_da_503_generico(self):
        with mock.patch("apps.quotes.views.search_all", side_effect=RuntimeError("detalle interno")), \
                self.assertLogs("apps.quotes", "ERROR"):
            respuesta = await self.buscar([])
        self.assertEqual(respuesta.status_code, 503)
        self.assertSinTraza(respuesta)
        self.assertNotIn(b"detalle interno", respuesta.content)

    async def test_v9_datos_que_m6_rechaza_dejan_fuera_solo_a_ese_proveedor(self):
        duplicados = adaptador("alfa", mxn("1", "100.00"), mxn("1", "90.00"))
        with self.assertLogs("apps.quotes", "WARNING"):
            respuesta = await self.buscar([duplicados, adaptador("beta", mxn("7", "50.00"))])
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["proveedores_caidos"], ["alfa"])
        self.assertEqual([o.oferta.provider_code for o in respuesta.context["ofertas"]], ["beta"])

        cache.clear()
        with self.assertLogs("apps.quotes", "WARNING"):
            todos = await self.buscar([duplicados, adaptador("beta", error=TimeoutError())])
        self.assertEqual(todos.status_code, 503)
        self.assertEqual(todos.context["proveedores_caidos"], ["alfa", "beta"])

    async def test_v10_confianza_por_proveedor_en_el_emparejamiento(self):
        categoria = await Category.objects.acreate(name="Redes sintéticas")
        canonico = await ProductoCanonico.objects.acreate(
            marca="Marca X", modelo="MOD-1", descripcion="Equipo sintético", categoria=categoria
        )
        await Provider.objects.acreate(code="syscom", name="Syscom", base_url="https://syscom.example.com")
        item = mxn("1", "100.00", marca="Marca X", modelo="MOD-1")
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", mock.AsyncMock(return_value=Decimal("18.54"))):
            respuesta = await self.buscar([
                adaptador("syscom", item), adaptador("alfa", item), adaptador("beta"),
            ])
        self.assertEqual(respuesta.status_code, 200)
        matches = {o.oferta.provider_code: o.match for o in respuesta.context["ofertas"]}
        self.assertEqual(matches["syscom"].estado, EstadoMatch.AUTO_MATCH)
        self.assertEqual(matches["syscom"].metodo, MetodoMatch.DETERMINISTICO_MODELO)
        self.assertEqual(matches["syscom"].canonico.pk, canonico.pk)
        # Un proveedor sin declaración de confianza no autoriza ninguna clave; sin categoría
        # tampoco hay fallback M5c.
        self.assertEqual(matches["alfa"].estado, EstadoMatch.SIN_MATCH)
        self.assertIsNone(matches["alfa"].canonico)
        self.assertEqual(matches["alfa"].motivo, "FALLBACK_SIN_CATEGORIA")

    async def test_v10b_categoria_llega_al_fallback_de_m5(self):
        categoria = await Category.objects.acreate(name="Switches")
        item = mxn("1", "100.00", marca="Marca X", nombre="Switch 24 puertos")
        with mock.patch("apps.quotes.views.aemparejar_con_fallback",
                        wraps=aemparejar_con_fallback) as emparejar:
            respuesta = await self.buscar(
                [adaptador("alfa", item), adaptador("beta")], categoria=str(categoria.pk)
            )
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(emparejar.await_args.kwargs["categoria_id"], categoria.pk)
        self.assertNotEqual(respuesta.context["ofertas"][0].match.motivo, "FALLBACK_SIN_CATEGORIA")

    async def test_v11_solo_get(self):
        llamadas = []
        with adaptadores_registrados(adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)), sin_red():
            respuesta = await self.async_client.post(reverse("cotizar_productos"), {"q": "camara"})
        self.assertEqual(respuesta.status_code, 405)
        self.assertEqual(llamadas, [])

    async def test_v13_categorias_activas_para_el_formulario_tambien_en_errores(self):
        await Category.objects.acreate(name="Redes")
        await Category.objects.acreate(name="Cámaras")
        await Category.objects.acreate(name="Descontinuada", is_active=False)
        respuesta = await self.buscar([adaptador("alfa", mxn("1", "1.00"))], tipo_cambio="17,50")
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual([c.name for c in respuesta.context["categorias"]], ["Cámaras", "Redes"])

    async def test_v14_nombres_legibles_de_proveedor(self):
        respuesta = await self.buscar([
            adaptador("alfa", mxn("1", "100.00")),
            adaptador("beta", error=TimeoutError()),
        ])
        self.assertEqual([o.proveedor_nombre for o in respuesta.context["ofertas"]], ["Alfa"])
        self.assertEqual(respuesta.context["proveedores_caidos"], ["beta"])
        self.assertEqual(respuesta.context["proveedores_caidos_nombres"], ["Beta"])

    @override_settings(OFERTAS_POR_PROVEEDOR=1)
    async def test_v12_solo_se_muestran_las_n_mas_relevantes_por_proveedor(self):
        # Todas coinciden con «camara ip»: manda el orden de cada proveedor, no el precio.
        respuesta = await self.buscar([
            adaptador("alfa", mxn("1", "300.00"), mxn("2", "100.00")),
            adaptador("beta", mxn("1", "200.00"), mxn("2", "250.00")),
        ])
        self.assertEqual(
            [(o.oferta.provider_code, o.oferta.external_id) for o in respuesta.context["ofertas"]],
            [("beta", "1"), ("alfa", "1")],
        )

    @override_settings(OFERTAS_POR_PROVEEDOR=10)
    async def test_v17_primero_lo_que_el_titulo_nombra_y_luego_los_relacionados(self):
        respuesta = await self.buscar([
            adaptador("alfa",
                      mxn("sim", "50.00", nombre="Reactivación de Conectividad / SIM"),
                      mxn("monitor", "900.00", nombre='Monitor LED 24" / HDMI'),
                      mxn("cable", "60.00", nombre="Cable HDMI 2 m"),
                      mxn("soporte", "100.00", nombre="Soporte de Pared para Monitor / VESA")),
            adaptador("beta", mxn("otro", "800.00", nombre='Monitor 27" / HDMI')),
        ], q="monitor")
        ofertas = respuesta.context["ofertas"]
        self.assertEqual([o.oferta.external_id for o in ofertas], ["otro", "monitor", "soporte", "sim", "cable"])
        self.assertEqual([o.es_mejor_precio for o in ofertas], [True, True, False, False, False])
        self.assertEqual([o.sin_coincidencia for o in ofertas], [False, False, False, True, True])
        html = respuesta.content.decode()
        self.assertEqual(html.count("Otros resultados relacionados"), 1)
        self.assertLess(html.index("Soporte de Pared para Monitor"), html.index("Otros resultados relacionados"))
        self.assertLess(html.index("Otros resultados relacionados"), html.index("Reactivación de Conectividad"))

    async def test_v20_foto_y_enlace_a_la_ficha_del_proveedor(self):
        respuesta = await self.buscar([
            adaptador("alfa", mxn("1", "100.00", foto="https://img.ejemplo.invalid/1.png",
                                  ficha="https://www.ejemplo.invalid/producto/1.html")),
            adaptador("beta", mxn("2", "200.00", nombre="Cámara IP sin foto")),
        ])
        self.assertContains(respuesta, '<img class="oferta-foto" src="https://img.ejemplo.invalid/1.png"')
        self.assertContains(respuesta, 'loading="lazy"')
        self.assertContains(respuesta, 'referrerpolicy="no-referrer"')
        self.assertContains(respuesta, 'href="https://www.ejemplo.invalid/producto/1.html" target="_blank" rel="noopener noreferrer"')
        self.assertContains(respuesta, "Ver en Alfa", count=1)
        self.assertNotContains(respuesta, "Ver en Beta")                       # beta no trae ficha: sin enlace
        self.assertContains(respuesta, "Sin foto", count=1)                    # beta no trae foto

    async def test_v21_tipo_de_cambio_como_frase_y_etiqueta_con_cambiar(self):
        """11B: sin tipo de cambio, el campo «1 USD = $ … MXN» con ejemplo; aplicado, etiqueta y «Cambiar»."""
        sin_tipo = await self.buscar([adaptador("alfa", mxn("1", "100.00"))])
        self.assertContains(sin_tipo, 'placeholder="Ej. 18.50"')
        self.assertContains(sin_tipo, "Obligatorio si el proveedor da precios en USD")
        self.assertNotContains(sin_tipo, 'class="fx-aplicado"')
        self.assertNotContains(sin_tipo, "<summary>Cambiar</summary>")

        con_tipo = await self.buscar([adaptador("alfa", usd("1", "10.25"))], tipo_cambio="17.50")
        self.assertEqual(con_tipo.status_code, 200)
        self.assertContains(con_tipo, "1 USD = $17.50 MXN")
        self.assertContains(con_tipo, "<summary>Cambiar</summary>")
        self.assertContains(con_tipo, 'value="17.50"', count=1)    # el valor viaja en la siguiente búsqueda
        self.assertContains(con_tipo, 'id="tipo_cambio"', count=1)
        self.assertNotContains(con_tipo, "MXN/USD (manual")         # la línea gris anterior ya no aparece

    async def test_v22_los_avisos_salen_como_ventana_emergente(self):
        """Error y proveedores caídos: aviso emergente con X; sin tipo de cambio, botón que lleva al campo."""
        sin_tipo = await self.buscar([adaptador("alfa", usd("1", "10.25")), adaptador("beta", mxn("1", "200.00"))])
        self.assertEqual(sin_tipo.status_code, 400)
        self.assertContains(sin_tipo, 'class="alert quote-error-toast fade show" role="alert"', status_code=400)
        self.assertContains(sin_tipo, "No se pudo buscar", status_code=400)
        self.assertContains(sin_tipo, 'data-enfocar="tipo_cambio">Escribir tipo de cambio</button>', status_code=400)
        self.assertContains(sin_tipo, 'data-bs-dismiss="alert"', status_code=400)
        self.assertNotContains(sin_tipo, "alert-danger", status_code=400)

        caido = await self.buscar([adaptador("alfa", mxn("1", "100.00")), adaptador("beta", error=TimeoutError())])
        self.assertContains(caido, "Algunos proveedores no respondieron")
        self.assertContains(caido, "comparador-aviso")
        self.assertNotContains(caido, "Escribir tipo de cambio")
        self.assertNotContains(caido, "alert-warning")
        # La flecha del título usa el verde azulado del sitio, ya no el naranja.
        self.assertContains(caido, 'bi-caret-right-fill" style="color: var(--cconor-eleccion);"')

    async def test_v23_el_tipo_de_cambio_se_recuerda_en_la_sesion(self):
        """Se reutiliza con su hora original; otro valor lo reemplaza, uno inválido no y vacío lo olvida."""
        adaptadores = [adaptador("alfa", usd("1", "10.00")), adaptador("beta", mxn("1", "200.00"))]
        primera = await self.buscar(adaptadores, tipo_cambio="17.50")
        self.assertEqual(primera.status_code, 200)
        capturado = primera.context["tipo_cambio"].fecha

        segunda = await self.buscar(adaptadores)  # sin enviarlo
        self.assertEqual(segunda.status_code, 200)
        self.assertEqual((segunda.context["tipo_cambio"].valor, segunda.context["tipo_cambio"].fecha), (Decimal("17.50"), capturado))
        self.assertContains(segunda, "se conserva durante tu sesión")
        self.assertNotContains(segunda, "Capturado otro día")
        self.assertContains(segunda, 'value="17.50"')                  # viaja en la siguiente búsqueda

        with sin_red():
            blanco = await self.async_client.get(reverse("cotizar_productos"), {"nueva": "1"})   # 22A: sin volver a la última
        self.assertContains(blanco, "1 USD = $17.50 MXN")

        await self.buscar(adaptadores, tipo_cambio="18.00")
        invalido = await self.buscar(adaptadores, tipo_cambio="18,00")
        self.assertEqual(invalido.status_code, 400)
        tercera = await self.buscar(adaptadores)
        self.assertEqual(tercera.context["tipo_cambio"].valor, Decimal("18.00"))

        vacio = await self.buscar(adaptadores, tipo_cambio="")
        self.assertEqual(vacio.status_code, 400)                        # USD sin tipo de cambio, como antes
        sesion = await self.async_client.asession()
        self.assertIsNone(await sesion.aget(CLAVE_TIPO_CAMBIO))

    async def test_v24_un_tipo_de_cambio_de_otro_dia_se_marca(self):
        sesion = await self.async_client.asession()
        await sesion.aset(CLAVE_TIPO_CAMBIO, {"valor": "17.50", "fecha": "2020-01-02T10:00:00+00:00"})
        await sesion.asave()
        with sin_red():
            respuesta = await self.async_client.get(reverse("cotizar_productos"))
        self.assertContains(respuesta, "1 USD = $17.50 MXN")
        self.assertContains(respuesta, "<strong>Capturado otro día:</strong> <span>confirma que siga vigente.</span>", html=False)
        self.assertContains(respuesta, 'capturado a mano <span title="2 ene 2020, 04:00">hace ')   # 24C; exacta en hora de CDMX
        self.assertContains(respuesta, " años</span> · se conserva")
        self.assertNotContains(respuesta, "UTC")

    async def test_v25_el_de_syscom_se_aplica_solo(self):
        """Lienzo fila 13: sin uno a mano, el de SYSCOM de hoy se aplica solo, se marca SYSCOM y se recuerda."""
        await Provider.objects.acreate(code="syscom", name="SYSCOM", base_url="https://syscom.example.com")
        consulta = mock.AsyncMock(return_value=Decimal("18.54"))
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", consulta), sin_red():
            blanco = await self.async_client.get(reverse("cotizar_productos"))
        self.assertContains(blanco, "1 USD = $18.54 MXN")
        self.assertContains(blanco, "SYSCOM (contado) · consultado ")
        self.assertContains(blanco, "se actualiza solo")
        self.assertNotContains(blanco, "SYSCOM hoy:")
        self.assertEqual(consulta.await_count, 1)                       # se guarda 30 min
        sesion = await self.async_client.asession()
        self.assertEqual((await sesion.aget(CLAVE_TIPO_CAMBIO))["origen"], "syscom")

        with mock.patch("apps.quotes.views.consultar_tipo_cambio", consulta):
            busqueda = await self.buscar([adaptador("syscom", usd("1", "10.00")), adaptador("alfa"), adaptador("beta")])
        self.assertEqual((busqueda.context["tipo_cambio"].valor, busqueda.context["tipo_cambio"].origen), (Decimal("18.54"), "syscom"))
        # El formulario reenvía el mismo valor: sigue siendo el automático, no uno a mano.
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", consulta):
            reenviado = await self.buscar([adaptador("syscom", usd("1", "10.00")), adaptador("alfa"), adaptador("beta")],
                                          tipo_cambio="18.54")
        self.assertEqual(reenviado.context["tipo_cambio"].origen, "syscom")

    async def test_v26_volver_a_syscom_manda_sobre_lo_escrito(self):
        await Provider.objects.acreate(code="syscom", name="SYSCOM", base_url="https://syscom.example.com")
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", mock.AsyncMock(return_value=Decimal("18.54"))), sin_red():
            manual = await self.async_client.get(reverse("cotizar_productos"), {"tipo_cambio": "19.00"})
            self.assertContains(manual, "1 USD = $19.00 MXN")
            self.assertContains(manual, "capturado a mano")
            self.assertContains(manual, "SYSCOM hoy: <strong>$18.54</strong>")
            self.assertContains(manual, 'name="usar_tipo_cambio" value="syscom"')
            vuelta = await self.async_client.get(reverse("cotizar_productos"), {"tipo_cambio": "19.00", "usar_tipo_cambio": "syscom"})
        self.assertContains(vuelta, "1 USD = $18.54 MXN")
        self.assertContains(vuelta, "SYSCOM (contado) · ")

    async def test_v27_si_syscom_no_responde_o_no_esta_activo(self):
        consulta = mock.AsyncMock(side_effect=httpx.ConnectError("sin conexión"))
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", consulta), sin_red():
            sin_syscom = await self.async_client.get(reverse("cotizar_productos"))
        self.assertNotContains(sin_syscom, 'class="fx-aplicado"')
        self.assertContains(sin_syscom, "Sin tipo de cambio de SYSCOM; escríbelo a mano.")
        self.assertEqual(consulta.await_count, 0)                       # sin SYSCOM activo no se consulta

        await Provider.objects.acreate(code="syscom", name="SYSCOM", base_url="https://syscom.example.com")
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", consulta), sin_red(), self.assertLogs("apps.quotes.views", "WARNING"):
            caido = await self.async_client.get(reverse("cotizar_productos"))
            otra_vez = await self.async_client.get(reverse("cotizar_productos"))
        self.assertContains(caido, "SYSCOM no respondió y no hay un valor de hoy; escríbelo a mano.")
        self.assertContains(otra_vez, "SYSCOM no respondió y no hay un valor de hoy; escríbelo a mano.")
        self.assertEqual(consulta.await_count, 1)                       # el fallo también se guarda (1 min)
        self.assertNotContains(otra_vez, 'class="fx-aplicado"')         # sin valor de SYSCOM no se inventa uno

    async def test_v27b_si_syscom_falla_se_usa_el_ultimo_valor_del_dia(self):
        """Punto 2 del lienzo fila 13: el último valor bueno de SYSCOM de hoy sigue sirviendo."""
        await Provider.objects.acreate(code="syscom", name="SYSCOM", base_url="https://syscom.example.com")
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", mock.AsyncMock(return_value=Decimal("18.54"))), sin_red():
            await self.async_client.get(reverse("cotizar_productos"))
        # Caduca el valor de 30 min; queda el del día.
        await cache.adelete(f"comparador:tipo_cambio_syscom:{settings.SYSCOM_TIPO_CAMBIO_CAMPO}")
        falla = mock.AsyncMock(side_effect=httpx.ConnectError("sin conexión"))
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", falla), sin_red(), self.assertLogs("apps.quotes.views", "WARNING"):
            respuesta = await self.async_client.get(reverse("cotizar_productos"))
        self.assertContains(respuesta, "1 USD = $18.54 MXN")
        self.assertEqual(falla.await_count, 1)

    async def test_v28_el_de_syscom_se_actualiza_solo_y_el_manual_no(self):
        await Provider.objects.acreate(code="syscom", name="SYSCOM", base_url="https://syscom.example.com")
        await self.guardar_tipo_cambio(valor="18.40", fecha=timezone.now().isoformat(), origen="syscom")
        consulta = mock.AsyncMock(return_value=Decimal("18.54"))
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", consulta), sin_red():
            nuevo = await self.async_client.get(reverse("cotizar_productos"))
        self.assertContains(nuevo, "1 USD = $18.54 MXN")
        self.assertNotContains(nuevo, "SYSCOM hoy:")
        self.assertEqual((await (await self.async_client.asession()).aget(CLAVE_TIPO_CAMBIO))["valor"], "18.54")

        await self.guardar_tipo_cambio(valor="18.40", fecha=timezone.now().isoformat(), origen="manual")
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", consulta), sin_red():
            manual = await self.async_client.get(reverse("cotizar_productos"))
        self.assertContains(manual, "1 USD = $18.40 MXN")
        self.assertContains(manual, "Volver a SYSCOM")

    async def guardar_tipo_cambio(self, **datos):
        sesion = await self.async_client.asession()
        await sesion.aset(CLAVE_TIPO_CAMBIO, datos)
        await sesion.asave()

    async def test_v29_sigue_vigente_oculta_el_aviso_hasta_otro_dia(self):
        """21A: «Sigue vigente» guarda cuándo se revisó; conserva valor, hora y origen; nunca cambia el valor."""
        ayer = (timezone.now() - timedelta(days=1)).isoformat()
        await self.guardar_tipo_cambio(valor="17.50", fecha=ayer, origen="manual")
        with sin_red():
            aviso = await self.async_client.get(reverse("cotizar_productos"), {"nueva": "1"})
            self.assertContains(aviso, 'name="usar_tipo_cambio" value="confirmar"')
            confirmado = await self.async_client.get(reverse("cotizar_productos"),
                                                     {"nueva": "1", "tipo_cambio": "17.50", "usar_tipo_cambio": "confirmar"})
        self.assertNotContains(confirmado, "Capturado otro día")
        self.assertContains(confirmado, "1 USD = $17.50 MXN")
        self.assertContains(confirmado, "confirmado hace un momento")
        guardado = await (await self.async_client.asession()).aget(CLAVE_TIPO_CAMBIO)
        self.assertEqual((guardado["valor"], guardado["fecha"], guardado["origen"]), ("17.50", ayer, "manual"))
        self.assertEqual(timezone.localdate(datetime.fromisoformat(guardado["confirmado"])), timezone.localdate())
        with sin_red():
            despues = await self.async_client.get(reverse("cotizar_productos"), {"nueva": "1"})
        self.assertNotContains(despues, "Capturado otro día")
        # Confirmado ayer vuelve a avisar hoy.
        await self.guardar_tipo_cambio(valor="17.50", fecha=ayer, origen="manual", confirmado=ayer)
        with sin_red():
            otra_vez = await self.async_client.get(reverse("cotizar_productos"), {"nueva": "1"})
        self.assertContains(otra_vez, "Capturado otro día")

    async def test_v30_de_otro_dia_el_manual_avisa_y_el_de_syscom_se_renueva(self):
        await Provider.objects.acreate(code="syscom", name="SYSCOM", base_url="https://syscom.example.com")
        ayer = (timezone.now() - timedelta(days=1)).isoformat()
        consulta = mock.AsyncMock(return_value=Decimal("18.54"))
        await self.guardar_tipo_cambio(valor="18.40", fecha=ayer, origen="manual")
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", consulta), sin_red():
            manual = await self.async_client.get(reverse("cotizar_productos"), {"nueva": "1"})
        self.assertContains(manual, "1 USD = $18.40 MXN")                                # sigue el aplicado
        self.assertContains(manual, "Capturado otro día")
        self.assertContains(manual, "Volver a SYSCOM ($18.54)")
        await self.guardar_tipo_cambio(valor="18.54", fecha=ayer, origen="syscom")         # mismo valor, otro día
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", consulta), sin_red():
            syscom = await self.async_client.get(reverse("cotizar_productos"), {"nueva": "1"})
        self.assertContains(syscom, "1 USD = $18.54 MXN")
        self.assertNotContains(syscom, "Capturado otro día")
        guardado = await (await self.async_client.asession()).aget(CLAVE_TIPO_CAMBIO)
        self.assertEqual(timezone.localdate(datetime.fromisoformat(guardado["fecha"])), timezone.localdate())

    async def test_v31_otro_dia_se_cuenta_en_hora_de_ciudad_de_mexico(self):
        """Capturado 16:30 y revisado 19:29 del mismo día en México: en UTC ya era otro día; no debe avisar."""
        ahora = datetime(2026, 10, 21, 1, 29, tzinfo=dt_timezone.utc)                 # 20/10 19:29 en México
        await self.guardar_tipo_cambio(valor="18.54", fecha="2026-10-20T22:30:00+00:00", origen="manual")
        with mock.patch("django.utils.timezone.now", return_value=ahora), sin_red():
            respuesta = await self.async_client.get(reverse("cotizar_productos"), {"nueva": "1"})
        self.assertNotContains(respuesta, "Capturado otro día")
        self.assertContains(respuesta, 'capturado a mano <span title="20 oct 2026, 16:30">hace 2 horas</span>')

    async def test_v31b_personal_ve_el_de_syscom_y_no_lo_puede_cambiar(self):
        """Lienzo fila 13: sin permiso de tipo de cambio se ignora lo escrito y no hay campo para escribirlo."""
        personal = await sync_to_async(crear_asesor)("personal_tc", tipo_cambio=False)
        await self.async_client.aforce_login(personal)
        await Provider.objects.acreate(code="syscom", name="SYSCOM", base_url="https://syscom.example.com")
        await self.guardar_tipo_cambio(valor="19.90", fecha=timezone.now().isoformat(), origen="manual")
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", mock.AsyncMock(return_value=Decimal("18.54"))):
            respuesta = await self.buscar([adaptador("syscom", usd("1", "10.00")), adaptador("alfa"), adaptador("beta")],
                                          tipo_cambio="25.00")
        self.assertEqual((respuesta.context["tipo_cambio"].valor, respuesta.context["tipo_cambio"].origen), (Decimal("18.54"), "syscom"))
        self.assertContains(respuesta, "Solo un Asesor o un Administrador puede cambiarlo.")
        self.assertNotContains(respuesta, 'id="tipo_cambio"')
        self.assertNotContains(respuesta, "Cambiar</summary>")

    async def test_v31c_personal_sin_syscom_no_ve_precios_en_dolares(self):
        personal = await sync_to_async(crear_asesor)("personal_tc", tipo_cambio=False)
        await self.async_client.aforce_login(personal)
        await Provider.objects.acreate(code="syscom", name="SYSCOM", base_url="https://syscom.example.com")
        falla = mock.AsyncMock(side_effect=httpx.ConnectError("sin conexión"))
        with mock.patch("apps.quotes.views.consultar_tipo_cambio", falla), self.assertLogs("apps.quotes.views", "WARNING"):
            respuesta = await self.buscar([adaptador("syscom", usd("1", "10.00")), adaptador("alfa"), adaptador("beta")])
        self.assertEqual(respuesta.status_code, 400)
        self.assertContains(respuesta, "No hay tipo de cambio de SYSCOM en este momento", status_code=400)
        self.assertContains(respuesta, "Sin tipo de cambio: SYSCOM no respondió", status_code=400)
        self.assertNotContains(respuesta, "Escribir tipo de cambio", status_code=400)

    async def test_v32_al_volver_se_muestra_la_ultima_busqueda(self):
        """22A: el menú (sin parámetros) lleva a la última búsqueda; «Nueva búsqueda» y un formulario vacío la olvidan."""
        adaptadores = [adaptador("alfa", mxn("1", "100.00")), adaptador("beta")]
        categoria = await Category.objects.acreate(name="QA categoría")
        await self.buscar(adaptadores, q="monitor", categoria=str(categoria.pk), tipo_cambio="17.50")
        with sin_red():
            volver = await self.async_client.get(reverse("cotizar_productos"))
        self.assertEqual(volver.status_code, 302)
        self.assertEqual(volver["Location"], f"{reverse('cotizar_productos')}?q=monitor&categoria={categoria.pk}&ultima=1")
        with adaptadores_registrados(*adaptadores), sin_red():
            restaurada = await self.async_client.get(volver["Location"])
        self.assertContains(restaurada, "Mostrando tu última búsqueda (hace un momento).")
        self.assertContains(restaurada, 'value="monitor"')
        sesion = await self.async_client.asession()                            # «hace N min» cuenta desde la búsqueda
        ultima = await sesion.aget(CLAVE_ULTIMA_BUSQUEDA)
        await sesion.aset(CLAVE_ULTIMA_BUSQUEDA, {**ultima, "fecha": (timezone.now() - timedelta(minutes=10)).isoformat()})
        await sesion.asave()
        with adaptadores_registrados(*adaptadores), sin_red():
            otra_vez = await self.async_client.get(reverse("cotizar_productos"), follow=True)
        self.assertContains(otra_vez, "Mostrando tu última búsqueda (hace 10 min).")
        with sin_red():
            nueva = await self.async_client.get(reverse("cotizar_productos"), {"nueva": "1"})
            self.assertEqual(nueva.status_code, 200)
            self.assertEqual((await self.async_client.get(reverse("cotizar_productos"))).status_code, 200)   # ya no redirige
        await self.buscar(adaptadores, q="switch")
        with sin_red():
            await self.async_client.get(reverse("cotizar_productos"), {"q": "", "sku": ""})                  # buscar vacío
            self.assertEqual((await self.async_client.get(reverse("cotizar_productos"))).status_code, 200)
        invalida = await self.buscar(adaptadores, q="switch", categoria="abc")                               # 400: no se guarda
        self.assertEqual(invalida.status_code, 400)
        with sin_red():
            self.assertEqual((await self.async_client.get(reverse("cotizar_productos"))).status_code, 200)

    def test_v33_fechas_relativas_igual_que_en_el_navegador(self):
        """24C: mismas reglas que window.cconorFechas; la exacta en hora de Ciudad de México."""
        ahora = datetime(2026, 10, 21, 1, 30, tzinfo=dt_timezone.utc)                 # 20/10 19:30 en México
        casos = [((2026, 10, 21, 1, 29, 40), "hace un momento"), ((2026, 10, 21, 1, 5), "hace 25 min"),
                 ((2026, 10, 21, 0, 0), "hace 1 hora"), ((2026, 10, 20, 22, 30), "hace 3 horas"),
                 ((2026, 10, 20, 5, 0), "hace 1 día"), ((2026, 10, 17, 18, 0), "hace 3 días"),
                 ((2026, 7, 1, 18, 0), "hace 3 meses"), ((2024, 10, 1, 18, 0), "hace 2 años")]
        with mock.patch("django.utils.timezone.now", return_value=ahora):
            for partes, esperado in casos:
                with self.subTest(esperado=esperado):
                    self.assertEqual(hace_texto(datetime(*partes, tzinfo=dt_timezone.utc)), esperado)
        self.assertEqual(fecha_exacta(datetime(2026, 10, 20, 22, 5, tzinfo=dt_timezone.utc)), "20 oct 2026, 16:05")

    async def test_v18_sin_relacionados_no_aparece_el_titulo_del_bloque(self):
        respuesta = await self.buscar([adaptador("alfa", mxn("1", "100.00"))])
        self.assertNotContains(respuesta, "Otros resultados relacionados")

    async def test_v19_la_busqueda_por_modelo_conserva_el_orden_por_precio(self):
        respuesta = await self.buscar([
            adaptador("alfa", mxn("1", "300.00", nombre="Sin relación"), mxn("2", "100.00", nombre="Otra cosa")),
        ], q="", sku="QA-1")
        self.assertEqual([o.oferta.external_id for o in respuesta.context["ofertas"]], ["2", "1"])
        self.assertNotContains(respuesta, "Otros resultados relacionados")

    async def test_v15_formulario_en_blanco_no_consulta_ni_cuenta_en_el_limite(self):
        llamadas = []
        adaptadores = [adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)]
        for _ in range(LIMITE_COTIZACIONES + 1):
            respuesta = await self.buscar(adaptadores, q="  ", sku="")
            self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(llamadas, [])
        self.assertIsNone(await cache.aget(clave_limite(self.asesor)))

    async def test_v16_el_menu_marca_el_comparador_como_activo(self):
        respuesta = await self.buscar([adaptador("alfa", mxn("1", "1.00"))])
        self.assertEqual(respuesta.resolver_match.url_name, "comparador")
        self.assertEqual(reverse("cotizar_productos"), reverse("comparador"))


@tag("django_db")
class ProteccionTests(TestCase):
    """P1–P6: login real de M7a (docs/28). Las páginas con precios exigen sesión."""

    def setUp(self):
        cache.clear()
        self.asesor = crear_asesor(password="clave-de-prueba")

    def test_p1_paginas_con_datos_redirigen_al_login_sin_consultar(self):
        llamadas = []
        with adaptadores_registrados(adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)), sin_red():
            for nombre in ("comparador", "cotizador", "resumen"):
                with self.subTest(pagina=nombre):
                    url = reverse(nombre)
                    respuesta = self.client.get(url, {"q": "camara"} if nombre == "comparador" else {})
                    self.assertRedirects(
                        respuesta, f"{reverse('login')}?next={url}%3Fq%3Dcamara" if nombre == "comparador"
                        else f"{reverse('login')}?next={url}",
                        fetch_redirect_response=False,
                    )
        self.assertEqual(llamadas, [])
        self.assertIsNone(cache.get(clave_limite(self.asesor)))
        self.assertIsNone(cache.get("cotizar:ip:127.0.0.1"))

    def test_p2_solo_el_login_es_publico_inicio_exige_sesion(self):
        self.assertRedirects(self.client.get(reverse("inicio")), f"{reverse('login')}?next={reverse('inicio')}",
                             fetch_redirect_response=False)
        respuesta = self.client.get(reverse("login"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "registration/login.html")

    def test_p3_login_y_logout(self):
        respuesta = self.client.post(reverse("login"), {
            "username": "asesor", "password": "clave-de-prueba", "next": reverse("comparador"),
        })
        self.assertRedirects(respuesta, reverse("comparador"))
        self.assertEqual(self.client.get(reverse("comparador")).status_code, 200)

        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)  # solo POST
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))
        self.assertEqual(self.client.get(reverse("comparador")).status_code, 302)

    def test_p4_credenciales_incorrectas(self):
        respuesta = self.client.post(reverse("login"), {"username": "asesor", "password": "otra"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Usuario o contraseña incorrectos.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_p5_el_cotizador_entrega_la_cookie_csrf(self):
        self.client.force_login(self.asesor)
        respuesta = self.client.get(reverse("cotizador"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("csrftoken", respuesta.cookies)

    def test_p6_un_usuario_inactivo_no_tiene_sesion(self):
        self.client.force_login(crear_asesor("baja", is_active=False))
        self.assertEqual(self.client.get(reverse("comparador")).status_code, 302)

    def test_p7_sin_sesion_no_hay_menu_ni_pie_y_con_sesion_menu_de_usuario(self):
        """13C: el acceso no muestra nada del sistema. 13B: iniciales y nombre que despliegan «Cerrar sesión»."""
        anonimo = self.client.get(reverse("login"))
        for texto in ("cconor-navbar", "cconor-footer", f'action="{reverse("logout")}"', f'href="{reverse("cotizador")}"'):
            self.assertNotContains(anonimo, texto)
        self.assertContains(anonimo, "Iniciar sesión")

        self.asesor.first_name, self.asesor.last_name = "Ana", "Asesora"
        self.asesor.save()
        self.client.force_login(self.asesor)
        con_sesion = self.client.get(reverse("inicio"))
        self.assertContains(con_sesion, 'class="cconor-iniciales" aria-hidden="true">AA</span>')
        self.assertContains(con_sesion, "<span>Ana</span>")
        self.assertContains(con_sesion, 'data-bs-toggle="dropdown"')
        self.assertContains(con_sesion, f"Sesión iniciada como {self.asesor.get_username()}")
        self.assertContains(con_sesion, f'<form method="post" action="{reverse("logout")}"')
        self.assertContains(con_sesion, "csrfmiddlewaretoken")
        self.assertContains(con_sesion, "Cerrar sesión")
        self.assertNotContains(con_sesion, "SALIR")
        # El botón del menú cierra la sesión de verdad.
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))
        self.assertEqual(self.client.get(reverse("comparador")).status_code, 302)

    def test_p8_inicio_es_el_panel_y_ya_no_hay_portada_publica(self):
        self.assertEqual(self.client.get(reverse("inicio")).status_code, 302)

        self.asesor.first_name = "Ana"
        self.asesor.save()
        self.client.force_login(self.asesor)
        con_sesion = self.client.get(reverse("inicio"))
        self.assertContains(con_sesion, 'id="homePanel"')
        self.assertContains(con_sesion, "Hola, Ana")
        self.assertContains(con_sesion, "js/inicio.js")
        self.assertContains(con_sesion, f'data-cotizador-url="{reverse("cotizador")}"')
        self.assertNotContains(con_sesion, "COTIZAR PRODUCTOS")

    def test_p9_sin_nombre_se_usa_el_usuario(self):
        self.client.force_login(self.asesor)
        respuesta = self.client.get(reverse("inicio"))
        self.assertContains(respuesta, f"Hola, {self.asesor.get_username()}")
        usuario = self.asesor.get_username()
        self.assertContains(respuesta, f'class="cconor-iniciales" aria-hidden="true">{usuario[:2].upper()}</span>')
        self.assertContains(respuesta, f"<span>{usuario}</span>")

    def test_p10_sin_barra_superior_y_pie_de_una_linea(self):
        """Plataforma interna: sin teléfono, correo ni redes arriba; el pie no tiene navegación ni contacto."""
        self.client.force_login(self.asesor)
        for nombre in ("inicio", "cotizador", "resumen", "comparador"):
            with self.subTest(pagina=nombre):
                respuesta = self.client.get(reverse(nombre))
                self.assertEqual(respuesta.status_code, 200)
                self.assertNotContains(respuesta, "cconor-topbar")
                self.assertNotContains(respuesta, 'href="tel:')
                self.assertNotContains(respuesta, "bi-facebook")
                self.assertContains(respuesta, "Distribuidores:")
                self.assertContains(respuesta, "Portal de Cotización Multi-Proveedor")
                self.assertNotContains(respuesta, "<h6>Navegación</h6>")
                self.assertNotContains(respuesta, "<h6>Contacto</h6>")
                self.assertNotContains(respuesta, "contacto@cconor.com")


class CondicionesPredeterminadasTests(TestCase):
    """M2: condiciones activas que el cotizador precarga en cada cotización nueva."""

    def setUp(self):
        from apps.quotes.models import DefaultCommercialCondition
        self.modelo = DefaultCommercialCondition
        self.client.force_login(crear_asesor())

    def condiciones_de_la_pagina(self):
        import json
        import re
        respuesta = self.client.get(reverse("cotizador"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("csrftoken", respuesta.cookies)
        bloque = re.search(r'<script id="defaultConditions" type="application/json">(.*?)</script>',
                           respuesta.content.decode(), re.S)
        self.assertIsNotNone(bloque)
        return json.loads(bloque.group(1)), respuesta

    def test_sin_condiciones_la_pagina_entrega_lista_vacia(self):
        self.assertEqual(self.condiciones_de_la_pagina()[0], [])

    def test_solo_activas_y_en_orden(self):
        self.modelo.objects.create(text="Segunda", order=2)
        self.modelo.objects.create(text="Inactiva", order=0, active=False)
        self.modelo.objects.create(text="  Primera  ", order=1)
        self.assertEqual(self.condiciones_de_la_pagina()[0], ["Primera", "Segunda"])

    def test_texto_con_marcado_no_rompe_la_pagina(self):
        texto = '</script><script>alert("x")</script> & "comillas"'
        self.modelo.objects.create(text=texto)
        condiciones, respuesta = self.condiciones_de_la_pagina()
        self.assertEqual(condiciones, [texto])
        self.assertNotContains(respuesta, '<script>alert("x")</script>', html=False)

    def test_validaciones_del_modelo(self):
        from django.core.exceptions import ValidationError
        from apps.quotes.models import MAX_CONDICIONES_PREDETERMINADAS
        for texto in ("", "   ", "Dos\nlíneas", "Retorno\rcarro", "x" * 6001):
            with self.subTest(texto=texto[:20]), self.assertRaises(ValidationError):
                self.modelo.objects.create(text=texto)
        for i in range(MAX_CONDICIONES_PREDETERMINADAS):
            self.modelo.objects.create(text=f"C{i}")
        with self.assertRaises(ValidationError):
            self.modelo.objects.create(text="Una de más")
        self.modelo.objects.create(text="Inactiva no cuenta", active=False)
        primera = self.modelo.objects.first()
        primera.text = "Editada"
        primera.save()  # Editar una activa existente no choca con el límite.
        self.assertEqual(self.modelo.objects.filter(active=True).count(), MAX_CONDICIONES_PREDETERMINADAS)

    def test_admin_lista_y_alta(self):
        self.modelo.objects.create(text="Visible en el admin", order=3)
        self.client.force_login(crear_asesor("admin_qa", is_staff=True, is_superuser=True))
        lista = self.client.get(reverse("admin:quotes_defaultcommercialcondition_changelist"))
        self.assertContains(lista, "Visible en el admin")
        alta = self.client.post(reverse("admin:quotes_defaultcommercialcondition_add"),
                                {"text": "Desde el admin", "order": "1", "active": "on"})
        self.assertEqual(alta.status_code, 302)
        invalida = self.client.post(reverse("admin:quotes_defaultcommercialcondition_add"),
                                    {"text": "Dos\r\nlíneas", "order": "2", "active": "on"})
        self.assertEqual(invalida.status_code, 200)  # El formulario muestra el error, no guarda.
        self.assertEqual(list(self.modelo.objects.values_list("text", flat=True)), ["Desde el admin", "Visible en el admin"])


class TasaIvaInicialTests(TestCase):
    """M10: el IVA inicial del cotizador sale de apps/quotes/taxes.py, no de la plantilla."""

    def setUp(self):
        self.client.force_login(crear_asesor())

    def campo_iva(self):
        import re
        respuesta = self.client.get(reverse("cotizador"))
        self.assertEqual(respuesta.status_code, 200)
        campo = re.search(r'<input id="taxRate"[^>]*>', respuesta.content.decode())
        self.assertIsNotNone(campo)
        return campo.group(0)

    def test_muestra_la_tasa_inicial_en_porcentaje(self):
        self.assertIn('value="16"', self.campo_iva())

    def test_si_cambia_la_tasa_inicial_cambia_la_pantalla(self):
        with mock.patch("apps.quotes.views.TASA_IVA_INICIAL", Decimal("0.085")):
            self.assertIn('value="8.5"', self.campo_iva())
