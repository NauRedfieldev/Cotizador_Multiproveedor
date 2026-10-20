"""M6 con proveedores ficticios: sin API, ORM ni equivalencias inferidas."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import MAX_EMAX, MIN_EMIN, Decimal, Inexact, Rounded, localcontext
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.providers.contracts import ProviderOffer, ProviderResult
from apps.providers.errors import ProviderTimeout
from apps.quotes.comparison import (
    NIVEL_SIN_COINCIDENCIA, TipoCambio, comparar, comparar_configurado, nivel_de_coincidencia,
    palabras_de_busqueda, tipo_cambio_manual,
)

FECHA = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def oferta(proveedor="ficticio", identificador="1", precio="1", moneda="MXN", **kwargs):
    return ProviderOffer(provider_code=proveedor, external_id=identificador, name="Equipo sintético",
                         price=Decimal(precio), currency=moneda, **kwargs)


def resultado(*ofertas, proveedor="ficticio"):
    return ProviderResult(provider_code=proveedor, offers=ofertas)


def cambio(valor="17.50"):
    return TipoCambio(valor=Decimal(valor), origen="manual", fecha=FECHA)


class ComparisonTests(SimpleTestCase):
    def test_agrupa_y_ordena_por_precio_no_por_posicion(self):
        a = resultado(oferta("nuevo", "2", "2"), oferta("nuevo", "1", "1"), proveedor="nuevo")
        b = resultado(oferta("otro", "1", "3"), proveedor="otro")
        comparacion = comparar([a, b])
        self.assertEqual(tuple(comparacion.por_proveedor), ("nuevo", "otro"))
        self.assertEqual([c.oferta.external_id for c in comparacion.por_proveedor["nuevo"]], ["1", "2"])
        self.assertEqual(comparacion, comparar([b, a]))
        self.assertEqual(comparacion.moneda, "MXN")
        self.assertIsNone(comparacion.tipo_cambio)

    def test_limite_predeterminado_diez(self):
        comparacion = comparar([resultado(*(oferta(identificador=str(i), precio=str(i)) for i in range(12)))])
        self.assertEqual(len(comparacion.por_proveedor["ficticio"]), 10)

    def test_limite_explicito_y_mayor_que_la_lista(self):
        datos = [resultado(oferta(), oferta(identificador="2", precio="2"))]
        self.assertEqual(len(comparar(datos, n=1).por_proveedor["ficticio"]), 1)
        self.assertEqual(len(comparar(datos, n=10).por_proveedor["ficticio"]), 2)

    @override_settings(OFERTAS_POR_PROVEEDOR=1)
    def test_configuracion_y_override_explicito(self):
        datos = [resultado(oferta(), oferta(identificador="2", precio="2"))]
        self.assertEqual(len(comparar_configurado(datos).por_proveedor["ficticio"]), 1)
        self.assertEqual(len(comparar_configurado(datos, n=2).por_proveedor["ficticio"]), 2)
        # El núcleo puro no depende de settings.
        self.assertEqual(len(comparar(datos).por_proveedor["ficticio"]), 2)

    def test_limite_invalido(self):
        for n in [0, -1, True, False, "3", Decimal("3"), 3.0, None]:
            with self.subTest(n=n), self.assertRaises((TypeError, ValueError)):
                comparar([], n=n)

    @override_settings(OFERTAS_POR_PROVEEDOR=0)
    def test_configuracion_invalida_no_se_ignora(self):
        with self.assertRaises(ValueError):
            comparar_configurado([])

    def test_stock_cero_y_desconocido_participan_sin_conversion(self):
        datos = resultado(oferta(identificador="agotado", precio="1", stock=0),
                          oferta(identificador="desconocido", precio="2", stock=None),
                          oferta(identificador="disponible", precio="3", stock=10))
        filas = comparar([datos]).por_proveedor["ficticio"]
        self.assertEqual([c.oferta.stock for c in filas], [0, None, 10])
        self.assertEqual([c.es_mejor_precio for c in filas], [True, False, False])

    def test_precio_cero_no_se_descarta(self):
        filas = comparar([resultado(oferta(precio="0", stock=0))]).por_proveedor["ficticio"]
        self.assertEqual(filas[0].precio_comparable, Decimal("0"))
        self.assertTrue(filas[0].es_mejor_precio)

    def test_conversion_cambia_orden_usd_mxn(self):
        datos = resultado(oferta(identificador="usd", precio="10", moneda="USD"),
                          oferta(identificador="mxn", precio="100"))
        tipo = cambio()
        comparacion = comparar([datos], tipo_cambio=tipo)
        filas = comparacion.por_proveedor["ficticio"]
        self.assertEqual([c.oferta.external_id for c in filas], ["mxn", "usd"])
        self.assertEqual(filas[1].precio_comparable, Decimal("175.00"))
        self.assertIs(comparacion.tipo_cambio, tipo)

    def test_mismo_tipo_para_todos_los_proveedores_usd(self):
        datos = [resultado(oferta("a", precio="2", moneda="USD"), proveedor="a"),
                 resultado(oferta("b", precio="3", moneda="USD"), proveedor="b")]
        comparacion = comparar(datos, tipo_cambio=cambio("20"))
        self.assertEqual(comparacion.por_proveedor["a"][0].precio_comparable, Decimal("40"))
        self.assertEqual(comparacion.por_proveedor["b"][0].precio_comparable, Decimal("60"))

    def test_mxn_no_se_convierte_si_hay_tipo(self):
        original = oferta(precio="382.533200")
        fila = comparar([resultado(original)], tipo_cambio=cambio()).por_proveedor["ficticio"][0]
        self.assertIs(fila.precio_comparable, original.price)

    def test_usd_sin_tipo_error_incluso_fuera_del_top_o_con_precio_cero(self):
        for precio in ["0", "100000"]:
            with self.subTest(precio=precio), self.assertRaisesMessage(ValueError, "tipo de cambio"):
                comparar([resultado(oferta(), oferta(identificador="usd", precio=precio, moneda="USD"))], n=1)

    def test_moneda_no_soportada_error_no_ranking_parcial(self):
        with self.assertRaisesMessage(ValueError, "moneda"):
            comparar([resultado(oferta(), oferta(identificador="eur", moneda="EUR"))], tipo_cambio=cambio())

    def test_no_redondea_seis_decimales_antes_de_ordenar(self):
        filas = comparar([resultado(oferta(identificador="a", precio="1.004999"),
                                   oferta(identificador="z", precio="1.004001"))]).por_proveedor["ficticio"]
        self.assertEqual([c.oferta.external_id for c in filas], ["z", "a"])
        self.assertEqual(filas[0].precio_comparable.as_tuple(), Decimal("1.004001").as_tuple())
        self.assertFalse(filas[1].es_mejor_precio)

    def test_conversion_conserva_todos_los_decimales(self):
        filas = comparar([resultado(oferta(precio="382.533200", moneda="USD"))],
                         tipo_cambio=cambio("17.123456")).por_proveedor["ficticio"]
        self.assertEqual(filas[0].precio_comparable, Decimal("6550.290418739200"))

    def test_captura_manual_alimenta_comparacion_y_conserva_contexto(self):
        tipo = tipo_cambio_manual("17.50", fecha=FECHA)
        comparacion = comparar([resultado(oferta(moneda="USD", precio="85.50"))], tipo_cambio=tipo)
        self.assertEqual(comparacion.por_proveedor["ficticio"][0].precio_comparable, Decimal("1496.2500"))
        self.assertEqual((comparacion.tipo_cambio.origen, comparacion.tipo_cambio.fecha), ("manual", FECHA))

    def test_nuevo_tipo_no_modifica_la_comparacion_anterior(self):
        datos = [resultado(oferta(moneda="USD", precio="10", identificador="usd"),
                           oferta(precio="180", identificador="mxn"))]
        anterior = comparar(datos, tipo_cambio=cambio("17"))
        nueva = comparar(datos, tipo_cambio=cambio("20"))
        self.assertEqual(anterior.por_proveedor["ficticio"][0].oferta.external_id, "usd")
        self.assertEqual(nueva.por_proveedor["ficticio"][0].oferta.external_id, "mxn")
        self.assertEqual(anterior.por_proveedor["ficticio"][0].precio_comparable, Decimal("170"))
        self.assertEqual(anterior.tipo_cambio.valor, Decimal("17"))

    def test_rango_decimal_no_representable_da_error_no_redondeo(self):
        for precio, tipo in [(Decimal(f"1E{MAX_EMAX}"), cambio("10")),
                             (Decimal(f"1E{MIN_EMIN}"), cambio("0.0000000001"))]:
            with self.subTest(precio=precio), self.assertRaisesMessage(ValueError, "rango decimal"):
                comparar([resultado(oferta(moneda="USD", precio=str(precio)))], tipo_cambio=tipo)

    def test_producto_exacto_no_depende_del_contexto_decimal_ambiente(self):
        datos = [resultado(oferta(precio="12345678901234567890.123456", moneda="USD"))]
        tipo = cambio("17.12345678901234567890")
        with localcontext() as esperado_ctx:
            esperado_ctx.prec = 100
            esperado = datos[0].offers[0].price * tipo.valor
        with localcontext() as ctx:
            ctx.prec = 3
            ctx.Emax = 10
            ctx.traps[Inexact] = True
            ctx.traps[Rounded] = True
            antes = ctx.copy()
            comparable = comparar(datos, tipo_cambio=tipo).por_proveedor["ficticio"][0].precio_comparable
            self.assertEqual(comparable.as_tuple(), esperado.as_tuple())
            self.assertEqual((ctx.prec, ctx.Emax, ctx.traps, ctx.flags),
                             (antes.prec, antes.Emax, antes.traps, antes.flags))

    def test_empates_exactos_y_limite_estricto(self):
        datos = resultado(oferta(identificador="b", precio="1.000000", stock=10),
                          oferta(identificador="a", precio="1", stock=0),
                          oferta(identificador="c", precio="2"))
        comparacion = comparar([datos])
        filas = comparacion.por_proveedor["ficticio"]
        self.assertEqual([c.oferta.external_id for c in filas], ["a", "b", "c"])
        self.assertEqual([c.es_mejor_precio for c in filas], [True, True, False])
        self.assertEqual(comparacion.proveedores_con_empate, frozenset({"ficticio"}))
        limitado = comparar([datos], n=1)
        self.assertEqual(len(limitado.por_proveedor["ficticio"]), 1)
        self.assertEqual(limitado.proveedores_con_empate, frozenset({"ficticio"}))

    def test_empate_despues_de_convertir(self):
        datos = resultado(oferta(identificador="usd", precio="1", moneda="USD"),
                          oferta(identificador="mxn", precio="17.50"))
        self.assertEqual(comparar([datos], tipo_cambio=cambio()).proveedores_con_empate, frozenset({"ficticio"}))

    def test_orden_de_entrada_no_cambia_resultado(self):
        a, b = oferta(identificador="a"), oferta(identificador="b")
        self.assertEqual(comparar([resultado(a, b)]), comparar([resultado(b, a)]))

    def test_error_del_proveedor_se_conserva_y_no_impide_otro(self):
        error = ProviderTimeout("caido", "Sin respuesta.")
        caido = ProviderResult(provider_code="caido", error=error)
        comparacion = comparar([caido, resultado(oferta())])
        self.assertEqual(tuple(comparacion.por_proveedor), ("ficticio",))
        self.assertIs(comparacion.errores["caido"], error)

    def test_ofertas_de_resultado_fallido_no_participan(self):
        caido = ProviderResult(provider_code="caido", offers=(oferta("caido", moneda="USD"),),
                               error=ProviderTimeout("caido", "Sin respuesta."))
        self.assertFalse(comparar([caido]).por_proveedor)

    def test_vacio_sin_ofertas_y_todos_caidos(self):
        self.assertFalse(comparar([]).por_proveedor)
        self.assertEqual(comparar([resultado()]).por_proveedor["ficticio"], ())
        caido = ProviderResult(provider_code="caido", error=ProviderTimeout("caido", "Sin respuesta."))
        self.assertEqual(tuple(comparar([caido]).errores), ("caido",))

    def test_incoherencia_de_proveedor_no_se_reasigna_silenciosamente(self):
        with self.assertRaisesMessage(ValueError, "proveedor"):
            comparar([resultado(oferta("otro"))])
        with self.assertRaisesMessage(ValueError, "proveedor"):
            comparar([ProviderResult("ficticio", error=ProviderTimeout("otro", "Error."))])

    def test_entradas_no_respetan_contratos(self):
        with self.assertRaises(TypeError):
            comparar(["resultado"])
        with self.assertRaises(TypeError):
            comparar([ProviderResult("ficticio", offers=("oferta",))])
        with self.assertRaises(TypeError):
            comparar([ProviderResult("ficticio", error="error")])
        for code in [None, "", " "]:
            with self.subTest(code=code), self.assertRaises(ValueError):
                comparar([ProviderResult(code)])

    def test_iterador_de_resultados_se_consume_una_vez(self):
        datos = [resultado(oferta())]
        self.assertEqual(comparar(iter(datos)), comparar(datos))

    def test_resultados_duplicados_no_sobrescriben_datos(self):
        with self.assertRaisesMessage(ValueError, "duplicado"):
            comparar([resultado(oferta()), resultado(oferta(identificador="2"))])

    def test_ids_duplicados_en_proveedor_no_se_eligen_arbitrariamente(self):
        with self.assertRaisesMessage(ValueError, "duplicado"):
            comparar([resultado(oferta(), oferta(stock=0))])
        # El mismo ID externo en distintos proveedores es válido.
        self.assertEqual(len(comparar([resultado(oferta()),
                                      resultado(oferta("otro"), proveedor="otro")]).por_proveedor), 2)

    def test_no_mutacion_ni_lectura_de_raw(self):
        class RawProhibido(dict):
            def __getitem__(self, key):
                raise AssertionError("No leer payload")

            def get(self, *args, **kwargs):
                raise AssertionError("No leer payload")

            def __iter__(self):
                raise AssertionError("No leer payload")

        original = oferta(moneda="USD", precio="382.533200", raw=RawProhibido(), stock=None)
        datos = [resultado(original)]
        fila = comparar(datos, tipo_cambio=cambio()).por_proveedor["ficticio"][0]
        self.assertIs(fila.oferta, original)
        self.assertEqual((original.price, original.currency, original.stock), (Decimal("382.533200"), "USD", None))
        self.assertIs(datos[0].offers[0], original)

    def test_estructuras_de_salida_no_editables(self):
        comparacion = comparar([resultado(oferta())])
        with self.assertRaises(TypeError):
            comparacion.por_proveedor["nuevo"] = ()
        with self.assertRaises(TypeError):
            comparacion.errores["nuevo"] = ProviderTimeout("nuevo", "Error.")
        with self.assertRaises(FrozenInstanceError):
            comparacion.moneda = "USD"
        with self.assertRaises(FrozenInstanceError):
            comparacion.por_proveedor["ficticio"][0].es_mejor_precio = False

    def test_no_consulta_bd_ni_red(self):
        with patch("socket.socket", side_effect=AssertionError("No usar red")):
            # SimpleTestCase prohíbe consultas a BD; settings no ejecuta ORM.
            comparar([resultado(oferta())])
            comparar_configurado([resultado(oferta())])


class TipoCambioTests(SimpleTestCase):
    def test_tipo_valido_inmutable_y_metadatos_conservados(self):
        tipo = cambio()
        self.assertEqual((tipo.valor, tipo.origen, tipo.fecha), (Decimal("17.50"), "manual", FECHA))
        with self.assertRaises(FrozenInstanceError):
            tipo.valor = Decimal("20")

    def test_origen_externo_es_dato_no_consulta_de_red(self):
        with patch("socket.socket", side_effect=AssertionError("No usar red")):
            tipo = TipoCambio(Decimal("17.123456"), "fuente_ficticia:normal", FECHA)
            comparacion = comparar([resultado(oferta(moneda="USD"))], tipo_cambio=tipo)
        self.assertEqual(comparacion.tipo_cambio.origen, "fuente_ficticia:normal")

    def test_tipo_manual_no_depende_de_precision_ambiente(self):
        with localcontext() as ctx:
            ctx.prec = 2
            tipo = tipo_cambio_manual("17.123456789", fecha=FECHA)
        self.assertEqual(tipo.valor.as_tuple(), Decimal("17.123456789").as_tuple())

    def test_tipo_decimal_positivo_finito(self):
        for valor in [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")]:
            with self.subTest(valor=valor), self.assertRaises(ValueError):
                TipoCambio(valor, "manual", FECHA)
        for valor in [17.5, 17, "17.50", True, None]:
            with self.subTest(valor=valor), self.assertRaises(TypeError):
                TipoCambio(valor, "manual", FECHA)

    def test_origen_y_fecha_validos_sin_inventarlos(self):
        for origen in ["", " "]:
            with self.subTest(origen=origen), self.assertRaises(ValueError):
                TipoCambio(Decimal("17.50"), origen, FECHA)
        with self.assertRaises(TypeError):
            TipoCambio(Decimal("17.50"), None, FECHA)
        for fecha in [None, "2026-10-12", FECHA.replace(tzinfo=None)]:
            with self.subTest(fecha=fecha), self.assertRaises((TypeError, ValueError)):
                TipoCambio(Decimal("17.50"), "manual", fecha)

    def test_captura_manual_texto_decimal(self):
        tipo = tipo_cambio_manual(" 17.123456 ", fecha=FECHA)
        self.assertEqual((tipo.valor, tipo.origen, tipo.fecha), (Decimal("17.123456"), "manual", FECHA))
        self.assertEqual(tipo_cambio_manual("18", fecha=FECHA).valor, Decimal("18"))

    def test_captura_manual_rechaza_formatos_ambiguos_o_no_positivos(self):
        for texto in ["", "17,50", "1,234.50", "17 50", "NaN", "Infinity", "1e2", "-1", "0", ".5"]:
            with self.subTest(texto=texto), self.assertRaises(ValueError):
                tipo_cambio_manual(texto, fecha=FECHA)
        for texto in [17.5, Decimal("17.50"), None]:
            with self.subTest(texto=texto), self.assertRaises(TypeError):
                tipo_cambio_manual(texto, fecha=FECHA)

    def test_comparador_no_acepta_tipo_suelto_sin_contexto(self):
        for tipo in [Decimal("17.50"), 17.5, "17.50"]:
            with self.subTest(tipo=tipo), self.assertRaises(TypeError):
                comparar([resultado(oferta())], tipo_cambio=tipo)


def titulada(identificador, precio, nombre, **kwargs):
    return ProviderOffer(provider_code="ficticio", external_id=identificador, name=nombre,
                         price=Decimal(precio), currency="MXN", **kwargs)


class CoincidenciaTests(SimpleTestCase):
    """Con término de búsqueda: primero lo que el título nombra (p. ej. «monitor» ≠ soportes)."""

    def nivel(self, nombre, termino="monitor", **kwargs):
        return nivel_de_coincidencia(titulada("1", "1", nombre, **kwargs), palabras_de_busqueda(termino))

    def test_niveles_por_donde_aparece_la_palabra(self):
        self.assertEqual(self.nivel('Monitor LED 32" Full HD / HDMI'), 0)
        self.assertEqual(self.nivel("Monitores Curvos 27 pulgadas"), 0)                  # plural
        self.assertEqual(self.nivel("Soporte de Pared Articulado para Monitor / VESA"), 1)
        self.assertEqual(self.nivel("Brazo de Escritorio para 2 Monitores / 17 a 27"), 1)
        self.assertEqual(self.nivel('Kit de Videoportero IP / Monitor Táctil 7"'), 2)
        self.assertEqual(self.nivel("Switch PoE+ Monitoreable / 4 Puertos"), NIVEL_SIN_COINCIDENCIA)
        self.assertEqual(self.nivel("Reactivación de Conectividad / SIM"), NIVEL_SIN_COINCIDENCIA)

    def test_acentos_plurales_palabras_vacias_marca_y_modelo(self):
        self.assertEqual(self.nivel("Cámara IP Bala 4 MP / Exterior", "camaras ip"), 0)
        self.assertEqual(self.nivel("Soporte de Pared para Monitor / VESA", "soporte para monitor"), 0)
        self.assertEqual(self.nivel("Bala 4 MP / Exterior", "hikvision bala", brand="HIKVISION"), 2)
        self.assertEqual(self.nivel("Bala 4 MP", "DS-2CD2043G2-I", model="DS-2CD2043G2-I"), 0)
        self.assertEqual(palabras_de_busqueda("  de la  "), ())
        self.assertEqual(self.nivel("Cualquier cosa", "de la"), 0)                      # sin palabras: sin niveles

    def test_elige_por_coincidencia_y_relevancia_y_ordena_por_precio(self):
        # Orden del proveedor (su relevancia): lo barato que no coincide no desplaza a los monitores.
        datos = [resultado(
            titulada("sim", "5", "Reactivación de Conectividad / SIM"),
            titulada("switch", "9", "Switch PoE+ Monitoreable / 4 Puertos"),
            titulada("m1", "900", 'Monitor LED 24" / HDMI'),
            titulada("soporte", "50", "Soporte de Pared para Monitor / VESA"),
            titulada("m2", "700", 'Monitor LED 27" / HDMI'),
            titulada("kit", "300", 'Kit de Videoportero / Monitor 7"'),
        )]
        filas = comparar(datos, n=4, termino="monitor").por_proveedor["ficticio"]
        self.assertEqual([f.oferta.external_id for f in filas], ["m2", "m1", "soporte", "kit"])
        self.assertEqual([f.nivel for f in filas], [0, 0, 1, 2])
        # «Mejor precio» entre los monitores (mejor nivel), no el soporte más barato.
        self.assertEqual([f.es_mejor_precio for f in filas], [True, False, False, False])

    def test_completa_con_relacionados_si_faltan_coincidencias(self):
        datos = [resultado(
            titulada("sim", "5", "Reactivación de Conectividad / SIM"),
            titulada("m1", "900", 'Monitor LED 24" / HDMI'),
            titulada("switch", "9", "Switch PoE+ Monitoreable"),
        )]
        filas = comparar(datos, n=3, termino="monitor").por_proveedor["ficticio"]
        self.assertEqual([(f.oferta.external_id, f.nivel) for f in filas],
                         [("m1", 0), ("sim", NIVEL_SIN_COINCIDENCIA), ("switch", NIVEL_SIN_COINCIDENCIA)])
        self.assertEqual([f.es_mejor_precio for f in filas], [True, False, False])

    def test_sin_ninguna_coincidencia_compara_entre_todas_y_detecta_empates(self):
        datos = [resultado(titulada("a", "5", "Cable HDMI"), titulada("b", "5", "Cable VGA"),
                           titulada("c", "9", "Adaptador"))]
        comparacion = comparar(datos, termino="monitor")
        filas = comparacion.por_proveedor["ficticio"]
        self.assertEqual([f.es_mejor_precio for f in filas], [True, True, False])
        self.assertIn("ficticio", comparacion.proveedores_con_empate)

    def test_sin_termino_conserva_las_mas_baratas(self):
        datos = [resultado(titulada("sim", "5", "Reactivación / SIM"), titulada("m1", "900", "Monitor LED"))]
        filas = comparar(datos, n=1).por_proveedor["ficticio"]
        self.assertEqual([(f.oferta.external_id, f.nivel, f.es_mejor_precio) for f in filas], [("sim", 0, True)])
