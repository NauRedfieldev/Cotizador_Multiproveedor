"""SDD M9 §8, pruebas X1–X6: extracción de formularios HTML con la biblioteca estándar (sin red)."""
from pathlib import Path

import httpx
from django.test import SimpleTestCase, tag

from apps.providers_web.base import CABECERAS_AJAX, unir_cookies
from apps.providers_web.extraccion import campos_de_formulario

MUESTRAS = Path(__file__).parent / "muestras"


@tag("unit")
class CamposDeFormularioTests(SimpleTestCase):
    def test_la_muestra_de_acceso_da_lo_que_enviaria_el_navegador(self):
        """X1 Ocultos, textos y la opción de cada select de Form1; sin botones ni campos de otro formulario."""
        # Arrange
        html = (MUESTRAS / "exel_acceso.html").read_text(encoding="utf-8")

        # Act
        campos = campos_de_formulario(html, "Form1")

        # Assert
        self.assertEqual(campos, {
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": "viewstate-de-muestra-no-real",
            "__VIEWSTATEGENERATOR": "A0B1C2D3",
            "__EVENTVALIDATION": "validacion-de-muestra-no-real",
            "ctl00$hdnIdCarrito_debugear": "",
            "ctl00$hdnMostrarMasterPopup": "0",
            "ctl00$hdnDirectorioRaiz": "https://www.exel.com.mx/xlstore/",
            "ctl00$hdnTokenCloudflare": "",
            "ctl00$ddlCategoria": "0",
            "ctl00$MainContent$hdnerror": "",
            "ctl00$MainContent$hdnToken": "",
            "ctl00$MainContent$txtUsuario": "",
            "ctl00$MainContent$txtPassword": "",
        })

    def test_sin_el_formulario_devuelve_none(self):
        """X2 Si la página no tiene el formulario pedido, None (no un diccionario vacío)."""
        # Act / Assert
        self.assertIsNone(campos_de_formulario('<form id="Otro"><input name="a" value="1"></form>', "Form1"))

    def test_un_formulario_vacio_devuelve_un_diccionario_vacio(self):
        """X2 Un formulario sin campos existe: {} y no None."""
        # Act / Assert
        self.assertEqual(campos_de_formulario('<form id="Form1"></form>', "Form1"), {})

    def test_decodifica_entidades_y_respeta_la_opcion_seleccionada(self):
        """X3 Las entidades de los atributos se decodifican y selected manda sobre la primera opción."""
        # Arrange
        html = (
            '<form id="Form1"><input type="hidden" name="v" value="a&amp;b&#233;">'
            '<select name="s"><option value="1">uno<option value="2" selected>dos</select></form>'
        )

        # Act
        campos = campos_de_formulario(html, "Form1")

        # Assert
        self.assertEqual(campos, {"v": "a&bé", "s": "2"})

    def test_tolera_etiquetas_sin_cerrar_y_tipos_en_mayusculas(self):
        """X4 HTML real mal formado: inputs sin cerrar, TYPE en mayúsculas, sin type y sin value."""
        # Arrange
        html = '<FORM ID="Form1"><input TYPE="HIDDEN" name="a" value="1"><input name="b"><input type="checkbox" name="c" value="x">'

        # Act
        campos = campos_de_formulario(html, "Form1")

        # Assert
        self.assertEqual(campos, {"a": "1", "b": ""})

    def test_solo_cuenta_el_primer_formulario_con_ese_id(self):
        """X4 Un id repetido no mezcla campos: vale el primer formulario."""
        # Arrange
        html = '<form id="F"><input type="hidden" name="a" value="1"></form><form id="F"><input type="hidden" name="b" value="2"></form>'

        # Act / Assert
        self.assertEqual(campos_de_formulario(html, "F"), {"a": "1"})


@tag("unit")
class CookiesYCabecerasTests(SimpleTestCase):
    def test_unir_cookies_junta_las_de_varias_respuestas_y_gana_la_ultima(self):
        """X5 Set-Cookie de varias respuestas → cabecera Cookie; la repetida toma el último valor y la vaciada se quita."""
        # Arrange
        primera = httpx.Response(200, headers=[
            ("set-cookie", "ASP.NET_SessionId=s1; path=/; HttpOnly"),
            ("set-cookie", "TS01=t1; Path=/"),
            ("set-cookie", "borrar=x; Path=/"),
        ])
        segunda = httpx.Response(302, headers=[
            ("set-cookie", "TS01=t2; Path=/"),
            ("set-cookie", "borrar=; expires=Thu, 01 Jan 1970 00:00:00 GMT"),
            ("set-cookie", "=sin-nombre"),
        ])

        # Act / Assert
        self.assertEqual(unir_cookies(primera, segunda), "ASP.NET_SessionId=s1; TS01=t2")
        self.assertEqual(unir_cookies(httpx.Response(200)), "")

    def test_las_cabeceras_ajax_son_las_de_jquery(self):
        """X6 Las llamadas a los servicios JSON se identifican como las de la propia página."""
        # Act / Assert
        self.assertEqual(CABECERAS_AJAX["X-Requested-With"], "XMLHttpRequest")
