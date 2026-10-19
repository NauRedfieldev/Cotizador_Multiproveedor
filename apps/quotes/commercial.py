"""Motor comercial puro: Decimal, sin ORM, HTTP, archivos ni autorización.

Las entradas son datos ya identificados por el consumidor, no payloads HTTP. Los
resultados son INTERNOS: incluyen costos y no deben exponerse al cliente tal cual.
Calcular no autoriza guardar/emitir ni sustituye permisos o confirmaciones.

El descuento es exclusivamente porcentual y global, sin reparto por partida.
Costo cero conocido es válido; venta/total cero generan avisos. Las confirmaciones
de usuarios internos autorizados corresponden al servicio, no a este cálculo.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum

from django.core.exceptions import ValidationError

from .comparison import TipoCambio
from .snapshot_values import MAX_ITEMS, MONEY_CONTEXT, decimal_limit

MARGEN_INICIAL = Decimal("35")
TASA_IVA_INICIAL = Decimal("0.16")
_CIEN = Decimal("100")
_UNO = Decimal("1")
_CERO = Decimal("0.00")


class CodigoAdvertencia(StrEnum):
    MARGEN_CERO = "MARGEN_CERO"
    COSTO_DESCONOCIDO = "COSTO_DESCONOCIDO"
    VENTA_BAJO_COSTO = "VENTA_BAJO_COSTO"
    VENTA_CERO = "VENTA_CERO"
    TOTAL_CERO = "TOTAL_CERO"
    # C16: solo partidas de proveedor (API); el stock no cambia importes.
    STOCK_CERO = "STOCK_CERO"
    STOCK_DESCONOCIDO = "STOCK_DESCONOCIDO"
    STOCK_INSUFICIENTE = "STOCK_INSUFICIENTE"


# Avisos que se muestran pero no se aceptan ni bloquean la emisión.
AVISOS_INFORMATIVOS = frozenset((CodigoAdvertencia.STOCK_CERO, CodigoAdvertencia.STOCK_INSUFICIENTE))
_STOCK_MAXIMO = 9223372036854775807


def _tasa(valor):
    # Fracción: .16 representa 16%. Mismo rango/precisión técnica que M8.
    return decimal_limit(valor, 5, 4, maximum=_UNO)


def _margen(valor):
    return decimal_limit(valor, 4, 2, maximum=Decimal("99.99"))


@dataclass(frozen=True, slots=True)
class PrecioCapturado:
    """Valor original por unidad; None en tasa_iva_incluido significa sin IVA.

    Si incluye IVA, su tasa es explícita y no se toma de la tasa de venta de la
    cotización. Originales/manuales conservan su valor: no se sustituyen por el neto.
    En P1 se reutiliza la precisión de origen M8 (18,6), no para los derivados.
    """

    valor: Decimal
    moneda: str = "MXN"
    tasa_iva_incluido: Decimal | None = None

    def __post_init__(self):
        decimal_limit(self.valor, 18, 6)
        if self.moneda not in ("MXN", "USD"):
            raise ValidationError("Solo MXN y USD están soportados.")
        if self.tasa_iva_incluido is not None:
            _tasa(self.tasa_iva_incluido)


@dataclass(frozen=True, slots=True)
class PartidaComercial:
    """Elegir margen O venta manual por unidad de venta, nunca ambos.

    El costo corresponde a la unidad original: se divide entre factor_conversion
    para obtener el costo de la unidad vendida. La venta manual ya corresponde a
    esa unidad vendida y NO se vuelve a dividir. Cantidad es un entero estricto.
    No recibe proveedor/raw ni inventa costo cero cuando es desconocido.
    Existencias van en la unidad del proveedor (None = desconocidas, no cero) y
    solo se evalúan si aplica_existencias (oferta de proveedor, no concepto manual).
    """

    referencia: str
    cantidad: int
    costo: PrecioCapturado | None
    margen: Decimal | None = None
    venta_manual: PrecioCapturado | None = None
    factor_conversion: Decimal = _UNO
    existencias: int | None = None
    aplica_existencias: bool = False

    def __post_init__(self):
        if not isinstance(self.referencia, str) or not self.referencia.strip() or len(self.referencia) > 6000:
            raise ValidationError("La partida requiere una referencia de texto válida.")
        if type(self.cantidad) is not int:
            raise ValidationError("La cantidad debe ser un entero, sin truncar decimales.")
        decimal_limit(Decimal(self.cantidad), 10, 2, minimum=_UNO)
        if self.costo is not None and not isinstance(self.costo, PrecioCapturado):
            raise TypeError("El costo debe ser PrecioCapturado o None.")
        decimal_limit(self.factor_conversion, 18, 6, minimum=Decimal("0.000001"))
        if type(self.aplica_existencias) is not bool:
            raise TypeError("aplica_existencias debe ser bool.")
        if self.existencias is not None and (type(self.existencias) is not int or not 0 <= self.existencias <= _STOCK_MAXIMO):
            raise ValidationError("Existencias fuera de rango o no enteras.")
        if (self.margen is None) == (self.venta_manual is None):
            raise ValidationError("Indica margen o venta manual, no ambos ni ninguno.")
        if self.margen is not None:
            _margen(self.margen)
            if self.costo is None:
                raise ValidationError("Sin costo conocido se necesita una venta manual.")
        elif not isinstance(self.venta_manual, PrecioCapturado):
            raise TypeError("La venta manual debe ser PrecioCapturado.")

    @classmethod
    def con_margen(cls, referencia, cantidad, costo, *, margen=MARGEN_INICIAL, factor_conversion=_UNO):
        """Default solo para partidas NUEVAS; al reconstruir capturas aportar el margen."""
        return cls(referencia, cantidad, costo, margen=margen, factor_conversion=factor_conversion)


@dataclass(frozen=True, slots=True)
class DescuentoGlobal:
    """Un descuento porcentual de 0 a 100 sobre la cotización antes de IVA.

    No admite importe fijo ni reparto entre partidas. Un total cero se calcula y
    genera aviso; los permisos y la confirmación de emisión son externos al motor.
    """

    porcentaje: Decimal

    def __post_init__(self):
        decimal_limit(self.porcentaje, 5, 2, maximum=_CIEN)


@dataclass(frozen=True, slots=True)
class AdvertenciaComercial:
    codigo: CodigoAdvertencia
    referencia: str | None  # None: advertencia sobre la cotización completa.

    @property
    def requiere_confirmacion(self):
        return self.codigo not in AVISOS_INFORMATIVOS


@dataclass(frozen=True, slots=True)
class ResultadoPartida:
    entrada: PartidaComercial
    costo_unitario_mxn: Decimal | None
    venta_unitaria_mxn: Decimal
    importe_costo: Decimal | None
    importe_venta: Decimal
    utilidad_antes_descuento: Decimal | None
    margen_real_antes_descuento: Decimal | None


@dataclass(frozen=True, slots=True)
class ResultadoComercial:
    partidas: tuple[ResultadoPartida, ...]
    subtotal_venta: Decimal
    descuento: Decimal
    subtotal_neto: Decimal
    tasa_iva: Decimal
    iva: Decimal
    total: Decimal
    costo_conocido: Decimal  # Suma parcial si alguna partida carece de costo.
    utilidad_bruta: Decimal | None  # None: incompleta, nunca utilidad ficticia.
    advertencias: tuple[AdvertenciaComercial, ...]


def _normalizar(precio, tipo_cambio):
    """Cociente Decimal exacto (numerador, denominador), sin redondear el neto."""
    valor = decimal_limit(precio.valor, 18, 6)
    if precio.moneda == "USD":
        if tipo_cambio is None:
            raise ValidationError("USD requiere un tipo de cambio explícito.")
        valor *= decimal_limit(tipo_cambio.valor, 18, 8, minimum=Decimal("0.00000001"))
    divisor = _UNO if precio.tasa_iva_incluido is None else _UNO + _tasa(precio.tasa_iva_incluido)
    return valor, divisor


def _importe(numerador, denominador=_UNO):
    """HALF_UP exacto del cociente, sin multiplicar un unitario periódico truncado.

    as_integer_ratio conserva exactamente los Decimal. El divmod de centavos evita
    que una división periódica anterior cambie un empate (.025 / 3 * 3 → .03).
    Todos los operandos ya están acotados; no convierte dinero a float.
    """
    n, nd = numerador.as_integer_ratio()
    d, dd = denominador.as_integer_ratio()
    centavos, resto = divmod(n * dd * 100, nd * d)
    if resto * 2 >= nd * d:
        centavos += 1
    return decimal_limit(Decimal(centavos).scaleb(-2), 38, 2)


def _calcular_partida(entrada, tipo_cambio):
    cantidad = Decimal(entrada.cantidad)
    costo_unitario = importe_costo = None
    if entrada.costo is not None:
        cn, cd = _normalizar(entrada.costo, tipo_cambio)
        cd *= decimal_limit(entrada.factor_conversion, 18, 6, minimum=Decimal("0.000001"))
        costo_unitario = cn / cd  # Solo representación; importe usa el cociente exacto.
        importe_costo = _importe(cn * cantidad, cd)
    if entrada.margen is not None:
        vn, vd = cn * _CIEN, cd * (_CIEN - _margen(entrada.margen))
    else:
        vn, vd = _normalizar(entrada.venta_manual, tipo_cambio)
    venta_unitaria = vn / vd
    importe_venta = _importe(vn * cantidad, vd)
    utilidad = margen_real = None
    if importe_costo is not None:
        utilidad = importe_venta - importe_costo
        if importe_venta:
            margen_real = utilidad * _CIEN / importe_venta
    return ResultadoPartida(entrada, costo_unitario, venta_unitaria, importe_costo,
                            importe_venta, utilidad, margen_real)


def _aviso_existencias(entrada):
    """Compara en unidad vendida: stock del proveedor × factor de conversión."""
    if not entrada.aplica_existencias:
        return None
    if entrada.existencias is None:
        return CodigoAdvertencia.STOCK_DESCONOCIDO
    if entrada.existencias == 0:
        return CodigoAdvertencia.STOCK_CERO
    if Decimal(entrada.existencias) * entrada.factor_conversion < Decimal(entrada.cantidad):
        return CodigoAdvertencia.STOCK_INSUFICIENTE
    return None


def calcular_cotizacion(
    partidas: Iterable[PartidaComercial], *, tipo_cambio: TipoCambio | None = None,
    descuento: DescuentoGlobal | None = None, tasa_iva: Decimal = TASA_IVA_INICIAL,
) -> ResultadoComercial:
    """Calcula importes internos, sin persistir, consultar APIs ni aprobar una emisión.

    Importes/costos por partida se redondean una vez a centavos, después se suman.
    Descuento sobre esa suma; IVA sobre el neto; utilidad sobre venta neta. Con
    descuento global, resultados individuales se etiquetan ANTES del descuento.
    No se atribuyen pérdidas por partida al descuento global. La autorización
    comercial y las confirmaciones de avisos quedan fuera de P1.
    """
    if tipo_cambio is not None:
        if not isinstance(tipo_cambio, TipoCambio):
            raise TypeError("Se requiere TipoCambio con valor, origen y fecha.")
        decimal_limit(tipo_cambio.valor, 18, 8, minimum=Decimal("0.00000001"))
    if descuento is not None and not isinstance(descuento, DescuentoGlobal):
        raise TypeError("Se requiere DescuentoGlobal o None.")
    tasa_iva = _tasa(tasa_iva)
    referencias, calculadas, advertencias = set(), [], []
    with localcontext(MONEY_CONTEXT):
        for entrada in partidas:
            if len(calculadas) >= MAX_ITEMS:
                raise ValidationError(f"Máximo {MAX_ITEMS} partidas por cotización.")
            if not isinstance(entrada, PartidaComercial):
                raise TypeError("Se requiere PartidaComercial.")
            if entrada.referencia in referencias:
                raise ValidationError("Referencia de partida duplicada; no se sobrescribe.")
            referencias.add(entrada.referencia)
            resultado = _calcular_partida(entrada, tipo_cambio)
            calculadas.append(resultado)
            if entrada.margen is not None and entrada.margen == 0:
                advertencias.append(AdvertenciaComercial(CodigoAdvertencia.MARGEN_CERO, entrada.referencia))
            if entrada.costo is None:
                advertencias.append(AdvertenciaComercial(CodigoAdvertencia.COSTO_DESCONOCIDO, entrada.referencia))
            if resultado.utilidad_antes_descuento is not None and resultado.utilidad_antes_descuento < 0:
                advertencias.append(AdvertenciaComercial(CodigoAdvertencia.VENTA_BAJO_COSTO, entrada.referencia))
            if resultado.importe_venta == 0:
                advertencias.append(AdvertenciaComercial(CodigoAdvertencia.VENTA_CERO, entrada.referencia))
            stock = _aviso_existencias(entrada)
            if stock is not None:
                advertencias.append(AdvertenciaComercial(stock, entrada.referencia))
        if not calculadas:
            raise ValidationError("Una cotización requiere al menos una partida.")
        subtotal = _importe(sum((p.importe_venta for p in calculadas), _CERO))
        costo_conocido = _importe(sum((p.importe_costo for p in calculadas if p.importe_costo is not None), _CERO))
        descuento_calculado = _CERO
        if descuento is not None:
            porcentaje = decimal_limit(descuento.porcentaje, 5, 2, maximum=_CIEN)
            descuento_calculado = _importe(subtotal * porcentaje, _CIEN)
        neto = subtotal - descuento_calculado
        iva = _importe(neto * tasa_iva)
        total = _importe(neto + iva)
        utilidad = neto - costo_conocido if all(p.importe_costo is not None for p in calculadas) else None
        if descuento_calculado and neto < costo_conocido:
            advertencias.append(AdvertenciaComercial(CodigoAdvertencia.VENTA_BAJO_COSTO, None))
        if total == 0:
            advertencias.append(AdvertenciaComercial(CodigoAdvertencia.TOTAL_CERO, None))
        return ResultadoComercial(
            tuple(calculadas), subtotal, descuento_calculado, neto, tasa_iva, iva, total,
            costo_conocido, utilidad, tuple(advertencias),
        )
