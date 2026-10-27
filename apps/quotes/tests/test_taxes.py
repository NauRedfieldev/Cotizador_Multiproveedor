"""M10: la regla fiscal vive en taxes.py y da exactamente los mismos importes que antes.

Motor puro: sin BD, red ni PDF.
Ejecutar: venv/bin/python -m unittest apps.quotes.tests.test_taxes -v
"""

from decimal import Decimal
from fractions import Fraction
import unittest

from django.core.exceptions import ValidationError

from apps.quotes import commercial, models, snapshots, taxes
from apps.quotes.commercial import DescuentoGlobal, PartidaComercial, PrecioCapturado, calcular_cotizacion
from apps.quotes.snapshot_values import MONEY_CONTEXT, amount
from apps.quotes.taxes import TASA_IVA_INICIAL, calcular_iva, divisor_iva_incluido, validar_tasa

D = Decimal
TASAS = [D("0"), D("0.0001"), D("0.08"), D("0.16"), D("0.165"), D("0.1650"), D("1")]


def iva_exacto(neto, tasa):
    """Oráculo independiente: neto × tasa en fracción exacta, HALF_UP a centavos."""
    centavos, resto = divmod(Fraction(neto) * Fraction(tasa) * 100, 1)
    if resto * 2 >= 1:
        centavos += 1
    return D(int(centavos)).scaleb(-2, MONEY_CONTEXT)


class CalcularIvaTests(unittest.TestCase):
    def test_igual_a_la_formula_anterior_centavo_a_centavo(self):
        # De 0.00 a 20.00 con varias tasas: incluye empates de medio centavo.
        # Compara contra el oráculo exacto y contra las dos fórmulas que había antes (v2 y v1).
        for centavos in range(2001):
            neto = D(centavos).scaleb(-2)
            for tasa in TASAS:
                with self.subTest(neto=neto, tasa=tasa):
                    esperado = iva_exacto(neto, tasa)
                    self.assertEqual(calcular_iva(neto, tasa), esperado)
                    self.assertEqual(commercial._importe(neto * tasa), esperado)  # v2 de antes
                    self.assertEqual(amount(neto * tasa), esperado)  # v1 de antes

    def test_empates_redondean_hacia_arriba(self):
        self.assertEqual(calcular_iva(D("0.25"), D("0.1")), D("0.03"))  # 0.025
        self.assertEqual(calcular_iva(D("0.05"), D("0.5")), D("0.03"))  # 0.025
        self.assertEqual(calcular_iva(D("0.03"), D("0.5")), D("0.02"))  # 0.015

    def test_importes_grandes_sin_perder_precision(self):
        neto = D("12345678901234567890123456789012.35")
        self.assertEqual(calcular_iva(neto, D("0.16")), iva_exacto(neto, D("0.16")))

    def test_rechaza_tasa_invalida_y_float(self):
        for tasa in (D("-0.01"), D("1.0001"), D("0.16001")):
            with self.subTest(tasa=tasa), self.assertRaises(ValidationError):
                calcular_iva(D("100.00"), tasa)
        with self.assertRaises(ValidationError):
            calcular_iva(D("100.00"), 0.16)


class ValidarTasaTests(unittest.TestCase):
    def test_acepta_de_cero_a_uno_con_hasta_cuatro_decimales(self):
        for tasa in (D("0"), D("0.16"), D("0.1650"), D("1"), D("1.0000")):
            with self.subTest(tasa=tasa):
                self.assertEqual(validar_tasa(tasa), tasa)

    def test_rechaza_fuera_de_rango_mas_decimales_no_finitos_y_float(self):
        for tasa in (D("-0.0001"), D("1.0001"), D("0.00001"), D("NaN"), D("Infinity"), 0.16, "0.16"):
            with self.subTest(tasa=tasa), self.assertRaises(ValidationError):
                validar_tasa(tasa)


class DivisorIvaIncluidoTests(unittest.TestCase):
    def test_sin_iva_divide_entre_uno(self):
        self.assertEqual(divisor_iva_incluido(None), D("1"))

    def test_con_iva_divide_entre_uno_mas_su_tasa(self):
        self.assertEqual(divisor_iva_incluido(D("0.16")), D("1.16"))
        self.assertEqual(divisor_iva_incluido(D("0.08")), D("1.08"))

    def test_rechaza_tasa_incluida_invalida(self):
        with self.assertRaises(ValidationError):
            divisor_iva_incluido(D("1.5"))


class UnaSolaFuenteTests(unittest.TestCase):
    def test_tasa_inicial_es_la_misma_en_todo_el_servidor(self):
        self.assertEqual(TASA_IVA_INICIAL, D("0.16"))
        self.assertIs(commercial.TASA_IVA_INICIAL, taxes.TASA_IVA_INICIAL)
        self.assertIs(snapshots.TASA_IVA_INICIAL, taxes.TASA_IVA_INICIAL)
        self.assertIs(models.DEFAULT_TAX_RATE, taxes.TASA_IVA_INICIAL)
        self.assertEqual(models.Quote._meta.get_field("tax_rate").default, TASA_IVA_INICIAL)

    def test_motor_comercial_usa_taxes(self):
        partida = PartidaComercial.con_margen("a", 3, PrecioCapturado(D("100")), margen=D("35"))
        resultado = calcular_cotizacion([partida], descuento=DescuentoGlobal(D("10")))
        self.assertEqual(resultado.tasa_iva, TASA_IVA_INICIAL)
        self.assertEqual(resultado.iva, calcular_iva(resultado.subtotal_neto, TASA_IVA_INICIAL))
        self.assertEqual(resultado.total, resultado.subtotal_neto + resultado.iva)

    def test_precio_con_iva_se_quita_con_su_propia_tasa(self):
        con_iva = PartidaComercial.con_margen("a", 1, PrecioCapturado(D("116"), "MXN", D("0.16")), margen=D("0"))
        sin_iva = PartidaComercial.con_margen("a", 1, PrecioCapturado(D("100")), margen=D("0"))
        self.assertEqual(calcular_cotizacion([con_iva], tasa_iva=D("0.08")).subtotal_venta,
                         calcular_cotizacion([sin_iva], tasa_iva=D("0.08")).subtotal_venta)


if __name__ == "__main__":
    unittest.main()
