"""Contrato HTTP de cotizar_productos: 200, 400 y 503, sin red.

La página en blanco y las validaciones salen antes de search_all. El 503 de
esta suite es el de cero proveedores activos, también antes de cualquier HTTP.
"""
from django.test import TestCase, tag
from django.urls import reverse

from apps.catalog.models import Category
from apps.providers.models import Provider
from apps.providers.tests.soporte import exigir_bd_de_pruebas, sin_red

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
