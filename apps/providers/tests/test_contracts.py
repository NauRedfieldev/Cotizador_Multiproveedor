"""SDD M2 §9, pruebas 1–10: contratos de datos (sin BD)."""
from decimal import Decimal

from django.test import SimpleTestCase, tag

from apps.providers.contracts import ProductQuery, ProviderOffer, ProviderResult
from apps.providers.errors import ProviderTimeout


def oferta(**campos):
    datos = {
        "provider_code": "demo",
        "external_id": "X1",
        "name": "Cámara IP",
        "price": Decimal("10.50"),
        "currency": "USD",
    }
    datos.update(campos)
    return ProviderOffer(**datos)


@tag("unit")
class PruebasProductQuery(SimpleTestCase):
    def test_recorta_term_y_convierte_un_sku_vacio_en_none(self):
        """1 Recorta term, convierte un sku vacío en None y usa limit=60 por defecto."""
        # Act
        consulta = ProductQuery(term=" laptop ", sku=" ")

        # Assert
        self.assertEqual((consulta.term, consulta.sku, consulta.limit), ("laptop", None, 60))

    def test_sin_term_ni_sku_lanza_value_error(self):
        """2 Una consulta sin term ni sku se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "La consulta requiere 'term' o 'sku'."):
            ProductQuery(term="  ")

    def test_limit_cero_lanza_value_error(self):
        """3 limit por debajo de 1 se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "limit debe estar entre 1 y 1000."):
            ProductQuery(term="x", limit=0)

    def test_limit_mayor_que_1000_lanza_value_error(self):
        """3 limit por encima de 1000 se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "limit debe estar entre 1 y 1000."):
            ProductQuery(term="x", limit=1001)

    def test_limit_que_no_es_entero_lanza_value_error(self):
        """3 limit tiene que ser un entero (un texto o un bool no valen)."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "limit debe estar entre 1 y 1000."):
            ProductQuery(term="x", limit=True)


@tag("unit")
class PruebasProviderOffer(SimpleTestCase):
    def test_raw_no_participa_en_la_igualdad(self):
        """4 Dos ofertas iguales salvo el raw son iguales."""
        # Act / Assert
        self.assertEqual(oferta(raw={"a": 1}), oferta(raw={"b": 2}))

    def test_raw_no_aparece_en_el_repr(self):
        """4 El raw no aparece en el repr (puede ser enorme o contener datos del proveedor)."""
        # Act
        texto = repr(oferta(raw={"clave": "valor-crudo"}))

        # Assert
        self.assertNotIn("valor-crudo", texto)

    def test_precio_float_lanza_type_error(self):
        """5 Un precio float se rechaza: el dinero va en Decimal."""
        # Act / Assert
        with self.assertRaisesMessage(TypeError, "price debe ser Decimal, no float."):
            oferta(price=10.5)

    def test_precio_negativo_lanza_value_error(self):
        """6 Un precio negativo se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "price debe ser un Decimal finito y >= 0."):
            oferta(price=Decimal("-1"))

    def test_precio_nan_lanza_value_error(self):
        """6 Un precio NaN se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "price debe ser un Decimal finito y >= 0."):
            oferta(price=Decimal("NaN"))

    def test_precio_infinito_lanza_value_error(self):
        """6 Un precio infinito se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "price debe ser un Decimal finito y >= 0."):
            oferta(price=Decimal("Infinity"))

    def test_moneda_en_minusculas_lanza_value_error(self):
        """7 La moneda tiene que ir en mayúsculas."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "currency debe ser un código ISO 4217 en mayúsculas."):
            oferta(currency="usd")

    def test_moneda_que_no_es_un_codigo_iso_lanza_value_error(self):
        """7 La moneda tiene que ser un código de tres letras."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "currency debe ser un código ISO 4217 en mayúsculas."):
            oferta(currency="PESOS")

    def test_external_id_vacio_lanza_value_error(self):
        """8 Una oferta sin external_id se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "external_id no puede estar vacío."):
            oferta(external_id="")

    def test_external_id_con_solo_espacios_lanza_value_error(self):
        """8 Un external_id con solo espacios se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "external_id no puede estar vacío."):
            oferta(external_id="   ")

    def test_stock_negativo_lanza_value_error(self):
        """9 Las existencias no pueden ser negativas."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "stock no puede ser negativo."):
            oferta(stock=-1)


@tag("unit")
class PruebasProviderResult(SimpleTestCase):
    def test_un_resultado_sin_error_es_ok_y_sin_descartes(self):
        """10 Por defecto un resultado es ok y no tiene ofertas descartadas."""
        # Act
        resultado = ProviderResult("demo")

        # Assert
        self.assertEqual((resultado.ok, resultado.discarded), (True, 0))

    def test_un_resultado_con_error_no_es_ok(self):
        """10 Un resultado con error no es ok."""
        # Act
        resultado = ProviderResult("demo", error=ProviderTimeout("demo", "Sin respuesta en 50 ms."))

        # Assert
        self.assertFalse(resultado.ok)
