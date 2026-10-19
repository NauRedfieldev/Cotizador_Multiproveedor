"""Contrato HTTP de cotizar_productos (M7, docs/26 y docs/28), sin red ni credenciales reales.

CotizarProductosTests (del equipo, M7b): página en blanco, validaciones y 503 sin
proveedores activos, todo antes de cualquier HTTP. VistaCotizacionTests (V1–V16): modo
degradado con adaptadores de prueba cuyo fetch no hace HTTP.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings, tag
from django.urls import reverse

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
from apps.quotes.views import LIMITE_COTIZACIONES

PLANTILLA = "comparador.html"


def crear_asesor(username="asesor", password=None, **campos):
    """Usuario sintético. Sin password no se calcula ningún hash (force_login no lo necesita)."""
    return get_user_model().objects.create_user(username, password=password, **campos)

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
        cache.clear()  # el rate limit es por IP y la LocMemCache dura toda la suite
        self.client.force_login(crear_asesor())
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
        self.async_client.force_login(crear_asesor())

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

    async def test_v7b_clave_caducada_entre_aadd_y_aincr_cuenta_como_primera(self):
        with mock.patch.object(cache, "aincr", side_effect=ValueError("Key not found")):
            respuesta = await self.buscar([adaptador("alfa", mxn("1", "1.00"))])
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(await cache.aget("cotizar:127.0.0.1"), 1)

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
    async def test_v12_solo_se_muestran_las_mejores_n_por_proveedor(self):
        respuesta = await self.buscar([
            adaptador("alfa", mxn("1", "300.00"), mxn("2", "100.00")),
            adaptador("beta", mxn("1", "200.00"), mxn("2", "250.00")),
        ])
        self.assertEqual(
            [(o.oferta.provider_code, o.oferta.external_id) for o in respuesta.context["ofertas"]],
            [("alfa", "2"), ("beta", "1")],
        )

    async def test_v15_formulario_en_blanco_no_consulta_ni_cuenta_en_el_limite(self):
        llamadas = []
        adaptadores = [adaptador("alfa", mxn("1", "1.00"), llamadas=llamadas)]
        for _ in range(LIMITE_COTIZACIONES + 1):
            respuesta = await self.buscar(adaptadores, q="  ", sku="")
            self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(llamadas, [])
        self.assertIsNone(await cache.aget("cotizar:127.0.0.1"))

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
        self.assertIsNone(cache.get("cotizar:127.0.0.1"))

    def test_p2_inicio_y_login_son_publicos(self):
        self.assertEqual(self.client.get(reverse("inicio")).status_code, 200)
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

    def test_p7_el_menu_ofrece_entrar_o_salir_segun_la_sesion(self):
        anonimo = self.client.get(reverse("inicio"))
        self.assertContains(anonimo, f'href="{reverse("login")}"')
        self.assertNotContains(anonimo, f'action="{reverse("logout")}"')

        self.asesor.first_name, self.asesor.last_name = "Ana", "Asesora"
        self.asesor.save()
        self.client.force_login(self.asesor)
        con_sesion = self.client.get(reverse("inicio"))
        self.assertContains(con_sesion, f'<form method="post" action="{reverse("logout")}"')
        self.assertContains(con_sesion, "csrfmiddlewaretoken")
        self.assertContains(con_sesion, "SALIR (Ana Asesora)")
        # El botón del menú cierra la sesión de verdad.
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))
        self.assertEqual(self.client.get(reverse("comparador")).status_code, 302)


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
