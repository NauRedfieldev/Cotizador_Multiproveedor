"""P1: motor puro, sin settings, BD, red, PDF ni cambios de datos históricos.

Ejecutar: venv/bin/python -m unittest apps.quotes.tests.test_commercial -v
"""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal, Inexact, ROUND_DOWN, Rounded, localcontext
from fractions import Fraction
import random
import unittest

from django.core.exceptions import ValidationError

from apps.quotes.commercial import (
    CodigoAdvertencia, DescuentoGlobal, PartidaComercial,
    PrecioCapturado, calcular_cotizacion,
)
from apps.quotes.comparison import TipoCambio

D = Decimal


def precio(valor, moneda="MXN", iva=None):
    return PrecioCapturado(D(valor), moneda, None if iva is None else D(iva))


def margen(costo="100", cantidad=1, porcentaje="35", referencia="a", factor="1"):
    return PartidaComercial.con_margen(referencia, cantidad, precio(costo),
                                      margen=D(porcentaje), factor_conversion=D(factor))


def manual(venta="100", costo="50", cantidad=1, referencia="a"):
    return PartidaComercial(referencia, cantidad, None if costo is None else precio(costo),
                           venta_manual=precio(venta))


def fx(valor="17.50"):
    return TipoCambio(D(valor), "QA sintético", datetime(2026, 10, 16, tzinfo=timezone.utc))


def redondeo_independiente(valor):
    """Oráculo racional de pruebas; no usa el algoritmo ni el contexto del motor."""
    centavos = valor * 100 + Fraction(1, 2)
    entero = centavos.numerator // centavos.denominator
    return D(f"{entero // 100}.{entero % 100:02d}")


class MargenYVentaTests(unittest.TestCase):
    def test_formula_es_margen_no_recargo(self):
        r = calcular_cotizacion([margen()])
        self.assertEqual((r.subtotal_venta, r.iva, r.total, r.utilidad_bruta),
                         (D("153.85"), D("24.62"), D("178.47"), D("53.85")))
        self.assertNotEqual(r.subtotal_venta, D("135"))
        self.assertEqual(r.advertencias, ())

    def test_default_solo_en_factory_de_nuevas_partidas(self):
        p = PartidaComercial.con_margen("a", 1, precio("100"))
        self.assertEqual(p.margen, D("35"))
        with self.assertRaises(ValidationError):
            PartidaComercial("a", 1, precio("100"))

    def test_margen_decimales_y_ceros_finales(self):
        a = calcular_cotizacion([margen(porcentaje="32.50")])
        b = calcular_cotizacion([margen(porcentaje="32.500000")])
        self.assertEqual(a, b)
        self.assertEqual(a.subtotal_venta, D("148.15"))

    def test_margen_cero_genera_advertencia_no_aprobacion(self):
        r = calcular_cotizacion([margen(porcentaje="0")])
        self.assertEqual((r.subtotal_venta, r.utilidad_bruta), (D("100"), D("0")))
        self.assertEqual([(a.codigo, a.referencia) for a in r.advertencias],
                         [(CodigoAdvertencia.MARGEN_CERO, "a")])
        self.assertFalse(hasattr(r, "puede_emitir"))

    def test_rechaza_margen_invalido(self):
        for valor in ("-0.01", "100", "101", "35.001", "NaN", "sNaN", "Infinity", "1e1000"):
            with self.subTest(valor=valor), self.assertRaises(ValidationError):
                margen(porcentaje=valor)

    def test_margen_99_99_valido_y_importes_acotados(self):
        r = calcular_cotizacion([margen(porcentaje="99.99")])
        self.assertEqual(r.subtotal_venta, D("1000000.00"))
        p = replace(margen("999999999999.999999", 99999999, "99.99", factor="0.000001"),
                    costo=precio("999999999999.999999", "USD"))
        with self.assertRaises(ValidationError):
            calcular_cotizacion([p], tipo_cambio=fx("9999999999.99999999"))

    def test_venta_manual_no_aplica_margen_automatico(self):
        r = calcular_cotizacion([manual("500", "100")])
        p = r.partidas[0]
        self.assertEqual((p.importe_venta, r.utilidad_bruta, p.margen_real_antes_descuento),
                         (D("500"), D("400"), D("80")))

    def test_venta_bajo_costo_genera_advertencia(self):
        r = calcular_cotizacion([manual("80", "100")])
        self.assertEqual((r.total, r.utilidad_bruta), (D("92.80"), D("-20")))
        self.assertEqual(r.partidas[0].margen_real_antes_descuento, D("-25"))
        self.assertEqual(r.advertencias[0].codigo, CodigoAdvertencia.VENTA_BAJO_COSTO)

    def test_venta_sin_costo_no_inventa_costo_ni_utilidad(self):
        p = manual("200", None)
        r = calcular_cotizacion([p])
        self.assertEqual((r.subtotal_venta, r.total), (D("200"), D("232")))
        self.assertIsNone(r.utilidad_bruta)
        self.assertIsNone(r.partidas[0].importe_costo)
        self.assertIsNone(r.partidas[0].costo_unitario_mxn)
        self.assertIsNone(r.partidas[0].utilidad_antes_descuento)
        self.assertIsNone(r.partidas[0].margen_real_antes_descuento)
        self.assertIsNone(p.costo)
        self.assertEqual(r.advertencias[0].codigo, CodigoAdvertencia.COSTO_DESCONOCIDO)

    def test_costo_parcial_no_se_presenta_como_utilidad_completa(self):
        r = calcular_cotizacion([manual("200", None), manual("100", "60", referencia="b")])
        self.assertEqual(r.costo_conocido, D("60"))
        self.assertIsNone(r.utilidad_bruta)
        self.assertEqual(r.partidas[1].utilidad_antes_descuento, D("40"))

    def test_costo_cero_conocido_es_distinto_de_ausente(self):
        r = calcular_cotizacion([manual("100", "0")])
        self.assertEqual(r.utilidad_bruta, D("100"))
        self.assertEqual(r.partidas[0].margen_real_antes_descuento, D("100"))
        self.assertEqual(r.advertencias, ())

    def test_venta_cero_no_divide_por_cero_ni_aprueba_gratuidad(self):
        r = calcular_cotizacion([manual("0", "10")])
        self.assertIsNone(r.partidas[0].margen_real_antes_descuento)
        self.assertEqual(r.utilidad_bruta, D("-10"))
        self.assertIn((CodigoAdvertencia.VENTA_CERO, "a"), [(a.codigo, a.referencia) for a in r.advertencias])
        self.assertIn((CodigoAdvertencia.TOTAL_CERO, None), [(a.codigo, a.referencia) for a in r.advertencias])

    def test_costo_y_venta_cero_generan_alerta_sin_inventar_perdida(self):
        r = calcular_cotizacion([manual("0", "0")])
        self.assertEqual((r.utilidad_bruta, r.total), (D("0"), D("0")))
        self.assertEqual([(a.codigo, a.referencia) for a in r.advertencias],
                         [(CodigoAdvertencia.VENTA_CERO, "a"), (CodigoAdvertencia.TOTAL_CERO, None)])

    def test_partida_gratuita_en_cotizacion_con_total_positivo_solo_alerta_de_partida(self):
        r = calcular_cotizacion([manual("0", "0"), manual(referencia="b")])
        self.assertEqual(r.total, D("116"))
        self.assertEqual([(a.codigo, a.referencia) for a in r.advertencias],
                         [(CodigoAdvertencia.VENTA_CERO, "a")])

    def test_modos_excluyentes_y_costo_requerido_para_margen(self):
        for campos in ({}, {"margen": D("35"), "venta_manual": precio("100")}):
            with self.subTest(campos=campos), self.assertRaises(ValidationError):
                PartidaComercial("a", 1, precio("100"), **campos)
        with self.assertRaises(ValidationError):
            PartidaComercial("a", 1, None, margen=D("35"))


class UnidadesIvaYFxTests(unittest.TestCase):
    def test_bobina_completa_y_metros(self):
        completa = calcular_cotizacion([margen("3050")])
        metros = calcular_cotizacion([margen("3050", 20, factor="305")])
        self.assertEqual(completa.subtotal_venta, D("4692.31"))
        self.assertEqual(metros.partidas[0].costo_unitario_mxn, D("10"))
        self.assertEqual((metros.costo_conocido, metros.subtotal_venta), (D("200"), D("307.69")))

    def test_unitario_periodico_no_se_redondea_antes_de_multiplicar(self):
        r = calcular_cotizacion([margen(cantidad=3)])
        self.assertEqual(r.subtotal_venta, D("461.54"))
        self.assertNotEqual(r.subtotal_venta, D("153.85") * 3)

    def test_empate_half_up_no_se_pierde_por_division_periodica(self):
        # .025 / 3 * 3 da .025 exacto, aunque el unitario tenga infinitos decimales.
        r = calcular_cotizacion([margen("0.025", 3, "0", factor="3")], tasa_iva=D("0"))
        self.assertEqual((r.costo_conocido, r.subtotal_venta), (D("0.03"), D("0.03")))

    def test_venta_manual_es_por_unidad_vendida_no_por_bobina(self):
        p = replace(manual("20", "3050", cantidad=10), factor_conversion=D("305"))
        r = calcular_cotizacion([p])
        self.assertEqual((r.costo_conocido, r.subtotal_venta), (D("100"), D("200")))

    def test_cantidad_entera_estricta_y_rango_m8(self):
        for cantidad in (0, -1, True, 1.5, D("1.00"), "2", 100000000):
            with self.subTest(cantidad=cantidad), self.assertRaises(ValidationError):
                margen(cantidad=cantidad)
        self.assertEqual(calcular_cotizacion([margen("1", cantidad=99999999)]).partidas[0].entrada.cantidad,
                         99999999)

    def test_factor_positivo_finito_y_precision(self):
        for factor in ("0", "-1", "NaN", "Infinity", "0.0000001", "1.0000001", "1e12"):
            with self.subTest(factor=factor), self.assertRaises(ValidationError):
                margen(factor=factor)

    def test_costo_con_iva_usa_tasa_de_compra_no_tasa_de_venta(self):
        p = PartidaComercial.con_margen("a", 1, precio("116", iva="0.16"), margen=D("0"))
        r = calcular_cotizacion([p], tasa_iva=D("0.08"))
        self.assertEqual((r.costo_conocido, r.subtotal_venta, r.iva, r.total),
                         (D("100"), D("100"), D("8"), D("108")))
        self.assertEqual(p.costo.valor, D("116"))
        self.assertEqual(p.costo.tasa_iva_incluido, D("0.16"))

    def test_venta_manual_con_iva_no_lo_aplica_dos_veces(self):
        p = replace(manual("200", "100"), venta_manual=precio("232", iva="0.16"))
        r = calcular_cotizacion([p])
        self.assertEqual((r.subtotal_venta, r.iva, r.total), (D("200"), D("32"), D("232")))

    def test_captura_sin_iva_no_se_divide_por_tasa_de_venta(self):
        r = calcular_cotizacion([manual("116", "50")])
        self.assertEqual((r.subtotal_venta, r.iva, r.total), (D("116"), D("18.56"), D("134.56")))

    def test_tasa_cero_incluida_es_explicita_y_valida(self):
        p = replace(manual(), venta_manual=precio("100", iva="0"))
        self.assertEqual(calcular_cotizacion([p]).subtotal_venta, D("100"))

    def test_usd_original_y_fx_no_se_sustituyen_por_mxn(self):
        p = PartidaComercial.con_margen("a", 2, precio("10.25", "USD"))
        tipo = fx()
        r = calcular_cotizacion([p], tipo_cambio=tipo)
        self.assertEqual((r.costo_conocido, r.subtotal_venta), (D("358.75"), D("551.92")))
        self.assertEqual((p.costo.valor, p.costo.moneda, tipo.valor), (D("10.25"), "USD", D("17.50")))

    def test_venta_manual_usd_sin_costo_tambien_requiere_fx(self):
        p = replace(manual(costo=None), venta_manual=precio("10", "USD"))
        with self.assertRaises(ValidationError):
            calcular_cotizacion([p])
        self.assertEqual(calcular_cotizacion([p], tipo_cambio=fx()).subtotal_venta, D("175"))

    def test_moneda_no_soportada_y_precision_original(self):
        for valor, moneda in (("1", "EUR"), ("1.0000001", "MXN"), ("1e12", "MXN"),
                              ("-1", "MXN"), ("NaN", "MXN"), ("Infinity", "USD")):
            with self.subTest(valor=valor, moneda=moneda), self.assertRaises(ValidationError):
                precio(valor, moneda)

    def test_fx_precision_explicita_sin_redondeo_silencioso(self):
        p = PartidaComercial.con_margen("a", 1, precio("1", "USD"))
        with self.assertRaises(ValidationError):
            calcular_cotizacion([p], tipo_cambio=fx("17.123456789"))
        with self.assertRaises(TypeError):
            calcular_cotizacion([p], tipo_cambio=D("17"))
        r = calcular_cotizacion([p], tipo_cambio=fx("17.12345678"))
        self.assertEqual(r.partidas[0].costo_unitario_mxn, D("17.12345678"))

    def test_tasas_invalidas(self):
        for valor in ("-0.01", "1.01", "0.16001", "NaN", "Infinity"):
            with self.subTest(valor=valor):
                with self.assertRaises(ValidationError):
                    calcular_cotizacion([manual()], tasa_iva=D(valor))
                with self.assertRaises(ValidationError):
                    precio("100", iva=valor)


class DescuentosYTotalesTests(unittest.TestCase):
    def test_descuento_global_porcentual_antes_del_iva(self):
        r = calcular_cotizacion([manual("1000", "600")], descuento=DescuentoGlobal(porcentaje=D("10")))
        self.assertEqual((r.subtotal_venta, r.descuento, r.subtotal_neto, r.iva, r.total, r.utilidad_bruta),
                         (D("1000"), D("100"), D("900"), D("144"), D("1044"), D("300")))
        self.assertEqual(r.partidas[0].utilidad_antes_descuento, D("400"))

    def test_descuento_se_aplica_despues_del_margen_y_redondea_half_up(self):
        r = calcular_cotizacion([margen()], descuento=DescuentoGlobal(porcentaje=D("10")))
        self.assertEqual((r.subtotal_venta, r.descuento, r.subtotal_neto, r.iva, r.total, r.utilidad_bruta),
                         (D("153.85"), D("15.39"), D("138.46"), D("22.15"), D("160.61"), D("38.46")))

    def test_descuento_decimal(self):
        r = calcular_cotizacion([manual("1000")], descuento=DescuentoGlobal(porcentaje=D("12.34")))
        self.assertEqual(r.descuento, D("123.40"))

    def test_total_cero_por_descuento_cien_genera_advertencia_no_autorizacion(self):
        r = calcular_cotizacion([manual()], descuento=DescuentoGlobal(porcentaje=D("100")))
        self.assertEqual((r.subtotal_neto, r.iva, r.total), (D("0"), D("0"), D("0")))
        self.assertIn((CodigoAdvertencia.TOTAL_CERO, None), [(a.codigo, a.referencia) for a in r.advertencias])
        self.assertFalse(hasattr(r, "puede_emitir"))

    def test_descuento_cero_no_genera_advertencias(self):
        r = calcular_cotizacion([manual(), manual(referencia="b")], descuento=DescuentoGlobal(porcentaje=D("0")))
        self.assertEqual(r.descuento, D("0"))
        self.assertEqual(r.advertencias, ())

    def test_reparto_de_descuento_no_se_inventa_ni_cambia_importes_individuales(self):
        r = calcular_cotizacion([manual(), manual(referencia="b")],
                               descuento=DescuentoGlobal(porcentaje=D("5")))
        self.assertEqual(r.subtotal_neto, D("190"))
        self.assertEqual([p.importe_venta for p in r.partidas], [D("100"), D("100")])
        self.assertEqual(r.utilidad_bruta, D("90"))
        self.assertEqual(r.advertencias, ())

    def test_control_global_no_atribuye_perdida_por_descuento_a_una_partida(self):
        r = calcular_cotizacion([manual("600", "580"), manual("400", "200", referencia="b")],
                               descuento=DescuentoGlobal(porcentaje=D("10")))
        self.assertEqual((r.subtotal_neto, r.utilidad_bruta), (D("900"), D("120")))
        self.assertEqual([p.utilidad_antes_descuento for p in r.partidas], [D("20"), D("200")])
        self.assertEqual(r.advertencias, ())

    def test_descuento_puede_generar_perdida_global_sin_partidas_en_perdida_previa(self):
        r = calcular_cotizacion([manual("600", "580"), manual("400", "390", referencia="b")],
                               descuento=DescuentoGlobal(porcentaje=D("10")))
        self.assertEqual(r.utilidad_bruta, D("-70"))
        self.assertEqual([(a.codigo, a.referencia) for a in r.advertencias],
                         [(CodigoAdvertencia.VENTA_BAJO_COSTO, None)])

    def test_descuento_que_causa_venta_bajo_costo_genera_advertencia_global(self):
        r = calcular_cotizacion([manual("110", "100")], descuento=DescuentoGlobal(porcentaje=D("20")))
        self.assertEqual(r.utilidad_bruta, D("-12"))
        self.assertEqual([(a.codigo, a.referencia) for a in r.advertencias],
                         [(CodigoAdvertencia.VENTA_BAJO_COSTO, None)])

    def test_descuento_y_costo_desconocido_no_fabrican_utilidad(self):
        r = calcular_cotizacion([manual(costo=None)], descuento=DescuentoGlobal(porcentaje=D("10")))
        self.assertEqual(r.subtotal_neto, D("90"))
        self.assertIsNone(r.utilidad_bruta)

    def test_costo_parcial_puede_demostrar_perdida_sin_inventar_el_faltante(self):
        r = calcular_cotizacion([manual("100", "100"), manual("10", None, referencia="b")],
                               descuento=DescuentoGlobal(porcentaje=D("50")))
        self.assertIsNone(r.utilidad_bruta)
        self.assertEqual(r.costo_conocido, D("100"))
        self.assertIn((CodigoAdvertencia.VENTA_BAJO_COSTO, None), [(a.codigo, a.referencia) for a in r.advertencias])

    def test_descuentos_invalidos(self):
        for porcentaje in (D("-1"), D("100.01"), D("10.001"), D("NaN"), D("Infinity"), None):
            with self.subTest(porcentaje=porcentaje), self.assertRaises(ValidationError):
                DescuentoGlobal(porcentaje=porcentaje)
        with self.assertRaises(TypeError):
            calcular_cotizacion([manual()], descuento=D("10"))

    def test_rechaza_importe_fijo_modalidades_acumuladas_y_porcentaje_omitido(self):
        for campos in ({}, {"importe_mxn": D("10")}, {"porcentaje": D("10"), "importe_mxn": D("10")}):
            with self.subTest(campos=campos), self.assertRaises(TypeError):
                DescuentoGlobal(**campos)

    def test_subtotal_suma_lineas_redondeadas_no_redondea_una_suma_cruda(self):
        r = calcular_cotizacion([manual("0.005", "0.001"), manual("0.005", "0.001", referencia="b")])
        self.assertEqual(r.subtotal_venta, D("0.02"))

    def test_suma_fuera_del_rango_de_importes(self):
        # Cada línea cabe en (38,2); la suma de 500 excede el límite de 36 enteros.
        p = replace(margen("999999999999", 99999999, "99.99"), costo=precio("999999999999", "USD"))
        with self.assertRaises(ValidationError):
            calcular_cotizacion((replace(p, referencia=str(i)) for i in range(500)), tipo_cambio=fx("9999999999"))


class ContratoYPurezaTests(unittest.TestCase):
    def test_rechaza_floats_en_todas_las_entradas_financieras(self):
        for constructor in (
            lambda: PrecioCapturado(10.1),
            lambda: PrecioCapturado(D("100"), tasa_iva_incluido=0.16),
            lambda: replace(margen(), margen=35.0),
            lambda: replace(margen(), factor_conversion=3.0),
            lambda: DescuentoGlobal(porcentaje=10.0),
            lambda: calcular_cotizacion([manual()], tasa_iva=0.16),
        ):
            with self.subTest(constructor=constructor), self.assertRaises(ValidationError):
                constructor()

    def test_contratos_de_registros_no_admiten_diccionarios(self):
        with self.assertRaises(TypeError):
            PartidaComercial("a", 1, {"valor": "100"}, margen=D("35"))
        with self.assertRaises(TypeError):
            PartidaComercial("a", 1, None, venta_manual=D("100"))
        with self.assertRaises(TypeError):
            calcular_cotizacion([{"costo": "100"}])

    def test_referencias_vacias_o_duplicadas_no_se_sobrescriben(self):
        for referencia in ("", " ", None, 1, "a" * 6001):
            with self.subTest(referencia=referencia), self.assertRaises(ValidationError):
                margen(referencia=referencia)
        with self.assertRaises(ValidationError):
            calcular_cotizacion([manual(), manual()])

    def test_limite_de_partidas_y_generadores(self):
        r = calcular_cotizacion(manual(referencia=str(i)) for i in range(500))
        self.assertEqual(len(r.partidas), 500)
        with self.assertRaises(ValidationError):
            calcular_cotizacion(manual(referencia=str(i)) for i in range(501))
        with self.assertRaises(ValidationError):
            calcular_cotizacion([])

    def test_resultados_e_inputs_inmutables_y_evidencia_preservada(self):
        p = margen("100.0000000")
        entradas = [p]
        r = calcular_cotizacion(entradas)
        entradas.clear()
        self.assertIs(r.partidas[0].entrada, p)
        self.assertEqual(str(p.costo.valor), "100.0000000")
        for objeto, atributo, valor in ((p, "cantidad", 2), (p.costo, "valor", D("0")), (r, "total", D("0"))):
            with self.subTest(atributo=atributo), self.assertRaises(FrozenInstanceError):
                setattr(objeto, atributo, valor)

    def test_contexto_del_llamador_no_sesga_resultados_ni_se_modifica(self):
        p = margen("0.025", 3, "0", factor="3")
        esperado = calcular_cotizacion([p])
        with localcontext() as contexto:
            contexto.prec = 3
            contexto.rounding = ROUND_DOWN
            contexto.Emax = 3
            contexto.Emin = -3
            contexto.traps[Inexact] = True
            contexto.traps[Rounded] = True
            contexto.clear_flags()
            flags = contexto.flags.copy()
            obtenido = calcular_cotizacion([p])
            self.assertEqual(obtenido, esperado)
            self.assertEqual((contexto.prec, contexto.rounding, contexto.Emax, contexto.Emin), (3, ROUND_DOWN, 3, -3))
            self.assertEqual(contexto.flags, flags)

    def test_precision_de_entradas_tambien_aislada_del_contexto(self):
        with localcontext() as contexto:
            contexto.prec = 2
            contexto.traps[Inexact] = True
            p = margen("123456.123456", porcentaje="35.12", factor="305")
            r = calcular_cotizacion([p], tasa_iva=D("0.16"))
        self.assertEqual(r.subtotal_venta, D("623.88"))

    def test_ceros_con_exponentes_extremos_se_normalizan_sin_inventar_precio(self):
        p = manual("100", "0E-1000000")
        r = calcular_cotizacion([p])
        self.assertEqual(r.costo_conocido, D("0"))
        self.assertEqual(r.utilidad_bruta, D("100"))

    def test_comparacion_con_oraculo_racional_sin_redondeos_intermedios(self):
        azar = random.Random(20261016)
        for i in range(200):
            costo = D(azar.randrange(1, 10000000)).scaleb(-6)
            porcentaje = D(azar.randrange(0, 10000)).scaleb(-2)
            factor = D(azar.choice([1, 3, 7, 29, 305]))
            cantidad = azar.randrange(1, 1000)
            iva_compra = D(azar.choice(["0", ".08", ".16"]))
            tipo = fx()
            p = PartidaComercial.con_margen(str(i), cantidad, PrecioCapturado(costo, "USD", iva_compra),
                                           margen=porcentaje, factor_conversion=factor)
            r = calcular_cotizacion([p], tipo_cambio=tipo)
            neto = Fraction(costo) * Fraction(tipo.valor) / ((1 + Fraction(iva_compra)) * Fraction(factor))
            venta = neto / (1 - Fraction(porcentaje) / 100)
            with self.subTest(i=i):
                self.assertEqual(r.partidas[0].importe_costo, redondeo_independiente(neto * cantidad))
                self.assertEqual(r.subtotal_venta, redondeo_independiente(venta * cantidad))

    def test_oraculo_en_extremos_de_precision_m8_y_factores_decimales(self):
        azar = random.Random(641868)
        for i in range(100):
            costo = D(azar.randrange(1, 10**18)).scaleb(-6)
            tipo = fx(str(D(azar.randrange(1, 10**18)).scaleb(-8)))
            factor = D(azar.randrange(1, 10**18)).scaleb(-6)
            porcentaje = D(azar.randrange(0, 10000)).scaleb(-2)
            cantidad = azar.randrange(1, 100000000)
            p = PartidaComercial.con_margen(str(i), cantidad, PrecioCapturado(costo, "USD", D("0.16")),
                                           margen=porcentaje, factor_conversion=factor)
            neto = Fraction(costo) * Fraction(tipo.valor) / (Fraction("1.16") * Fraction(factor))
            venta = neto / (1 - Fraction(porcentaje) / 100)
            r = calcular_cotizacion([p], tipo_cambio=tipo)
            with self.subTest(i=i):
                self.assertEqual(r.costo_conocido, redondeo_independiente(neto * cantidad))
                self.assertEqual(r.subtotal_venta, redondeo_independiente(venta * cantidad))

    def test_fecha_fx_no_se_inventa_y_precisiones_maximas_no_se_cuantizan_a_centavos(self):
        tipo = fx("17.12345678")
        p = PartidaComercial.con_margen("a", 1, precio("382.533200", "USD"))
        r = calcular_cotizacion([p], tipo_cambio=tipo)
        self.assertEqual(r.partidas[0].costo_unitario_mxn, D("6550.290717115096"))
        self.assertEqual(r.costo_conocido, D("6550.29"))
        self.assertEqual(tipo.fecha, datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertEqual(tipo.origen, "QA sintético")



def con_stock(existencias, cantidad=1, factor="1", aplica=True, referencia="a"):
    return PartidaComercial("%s" % referencia, cantidad, precio("100"), margen=D("35"), factor_conversion=D(factor),
                            existencias=existencias, aplica_existencias=aplica)


class AvisosDeStockTests(unittest.TestCase):
    """C16: stock 0 avisa, desconocido pide comprobación, insuficiente advierte."""

    def stock(self, partida):
        r = calcular_cotizacion([partida])
        return [(str(w.codigo), w.requiere_confirmacion) for w in r.advertencias if str(w.codigo).startswith("STOCK_")]

    def test_casos_c16_y_quien_requiere_confirmacion(self):
        self.assertEqual(self.stock(con_stock(None)), [("STOCK_DESCONOCIDO", True)])
        self.assertEqual(self.stock(con_stock(0, cantidad=5)), [("STOCK_CERO", False)])  # Sin doble aviso.
        self.assertEqual(self.stock(con_stock(4, cantidad=5)), [("STOCK_INSUFICIENTE", False)])
        self.assertEqual(self.stock(con_stock(5, cantidad=5)), [])  # Límite exacto no avisa.
        self.assertEqual(self.stock(con_stock(6, cantidad=5)), [])

    def test_compara_en_unidad_vendida_con_factor(self):
        # 2 rollos de 305 m = 610 m disponibles.
        self.assertEqual(self.stock(con_stock(2, cantidad=610, factor="305")), [])
        self.assertEqual(self.stock(con_stock(2, cantidad=611, factor="305")), [("STOCK_INSUFICIENTE", False)])
        # Factor fraccionario: 3 cajas × 0.5 = 1.5 unidades vendidas.
        self.assertEqual(self.stock(con_stock(3, cantidad=2, factor="0.5")), [("STOCK_INSUFICIENTE", False)])
        self.assertEqual(self.stock(con_stock(4, cantidad=2, factor="0.5")), [])

    def test_conceptos_manuales_y_default_no_evaluan_stock(self):
        self.assertEqual(self.stock(con_stock(None, aplica=False)), [])
        self.assertEqual(self.stock(con_stock(0, aplica=False)), [])
        self.assertEqual(self.stock(margen()), [])

    def test_stock_no_cambia_importes(self):
        base = calcular_cotizacion([margen(cantidad=3)])
        con = calcular_cotizacion([con_stock(0, cantidad=3)])
        self.assertEqual((base.subtotal_venta, base.iva, base.total, base.utilidad_bruta),
                         (con.subtotal_venta, con.iva, con.total, con.utilidad_bruta))

    def test_existencias_tipos_y_rango(self):
        for valor in (True, -1, 1.0, D("1"), "1", 9223372036854775808):
            with self.subTest(valor=valor), self.assertRaises(ValidationError):
                con_stock(valor)
        self.assertEqual(self.stock(con_stock(9223372036854775807, cantidad=1)), [])
        with self.assertRaises(TypeError):
            con_stock(1, aplica=1)

    def test_avisos_previos_siguen_requiriendo_confirmacion(self):
        r = calcular_cotizacion([margen(porcentaje="0")])
        self.assertTrue(all(w.requiere_confirmacion for w in r.advertencias))


if __name__ == "__main__":
    unittest.main()
