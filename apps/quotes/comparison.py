"""M6: ranking por proveedor en MXN, sin ORM, red ni persistencia.

Stock 0 y None participan y permanecen distintos. Mejor precio no garantiza
disponibilidad ni equivalencia de productos. No se cuantiza antes de ordenar.
El consumidor valida qué ofertas son alternativas y conserva el contexto de FX.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import (
    MAX_EMAX, MIN_EMIN, Context, Decimal, DecimalException, DivisionByZero,
    Inexact, InvalidOperation, Overflow, Rounded, Underflow, localcontext,
)
from types import MappingProxyType

from apps.providers.contracts import ProviderOffer, ProviderResult
from apps.providers.errors import ProviderError

DEFAULT_OFERTAS_POR_PROVEEDOR = 3


@dataclass(frozen=True, slots=True)
class TipoCambio:
    """MXN por USD, con origen y fecha explícitos; sin fecha automática ni caché."""

    valor: Decimal
    origen: str
    fecha: datetime

    def __post_init__(self):
        if not isinstance(self.valor, Decimal):
            raise TypeError("El tipo de cambio debe ser Decimal, nunca float.")
        if not self.valor.is_finite() or self.valor <= 0:
            raise ValueError("El tipo de cambio debe ser positivo y finito.")
        if not isinstance(self.origen, str):
            raise TypeError("El origen del tipo de cambio debe ser texto.")
        if not self.origen.strip():
            raise ValueError("El tipo de cambio requiere un origen explícito.")
        if not isinstance(self.fecha, datetime):
            raise TypeError("La fecha de captura debe ser datetime.")
        if self.fecha.utcoffset() is None:
            raise ValueError("La fecha de captura debe incluir zona horaria.")


def tipo_cambio_manual(texto: str, *, fecha: datetime) -> TipoCambio:
    """Entrada manual con punto decimal, sin separadores de miles ni paso por float.

    La futura vista aporta texto y fecha de captura; aquí no se crea un formulario.
    No interpretar comas silenciosamente: '17,50' y '1,750' son ambiguos.
    """
    if not isinstance(texto, str):
        raise TypeError("La captura manual debe llegar como texto decimal.")
    limpio = texto.strip()
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", limpio) is None:
        raise ValueError("Usa un decimal positivo con punto, sin separadores de miles.")
    return TipoCambio(Decimal(limpio), "manual", fecha)


@dataclass(frozen=True, slots=True)
class OfertaComparada:
    oferta: ProviderOffer
    precio_comparable: Decimal
    es_mejor_precio: bool


@dataclass(frozen=True, slots=True)
class ResultadoComparacion:
    por_proveedor: Mapping[str, tuple[OfertaComparada, ...]]
    errores: Mapping[str, ProviderError]
    proveedores_con_empate: frozenset[str]
    tipo_cambio: TipoCambio | None
    moneda: str = field(default="MXN", init=False)

    def __post_init__(self):
        # Copias propias y contenedores no editables. Las ofertas/errores son los
        # originales de M2: no se clona ni interpreta raw, ni se mutan excepciones.
        object.__setattr__(self, "por_proveedor", MappingProxyType({
            code: tuple(filas) for code, filas in sorted(self.por_proveedor.items())
        }))
        object.__setattr__(self, "errores", MappingProxyType(dict(sorted(self.errores.items()))))
        object.__setattr__(self, "proveedores_con_empate", frozenset(self.proveedores_con_empate))


def _multiplicar_exacto(precio: Decimal, tipo: Decimal) -> Decimal:
    # El producto de dos coeficientes requiere como máximo la suma de sus dígitos.
    # Context propio: ni precision/traps/flags ni Emax del llamador deben sesgarlo.
    contexto = Context(
        prec=len(precio.as_tuple().digits) + len(tipo.as_tuple().digits),
        Emax=MAX_EMAX, Emin=MIN_EMIN, clamp=0,
        traps=[InvalidOperation, DivisionByZero, Overflow, Underflow, Inexact, Rounded],
    )
    try:
        with localcontext(contexto):
            return precio * tipo
    except DecimalException as exc:
        raise ValueError("El precio convertido excede el rango decimal representable.") from exc


def _precio_comparable(oferta: ProviderOffer, tipo_cambio: TipoCambio | None) -> Decimal:
    if oferta.currency == "MXN":
        return oferta.price
    if oferta.currency != "USD":
        raise ValueError("M6 solo admite moneda MXN o USD; no se generó un ranking parcial.")
    if tipo_cambio is None:
        raise ValueError("Las ofertas USD requieren un tipo de cambio explícito.")
    return _multiplicar_exacto(oferta.price, tipo_cambio.valor)


def comparar(
    resultados: Iterable[ProviderResult],
    *,
    tipo_cambio: TipoCambio | None = None,
    n: int = DEFAULT_OFERTAS_POR_PROVEEDOR,
) -> ResultadoComparacion:
    """Núcleo puro: mejores precios por proveedor, con todos los estados de stock.

    Errores M2 se conservan sin usar sus ofertas. Un error de validación del
    comparador sí se lanza: no ocultar USD sin convertir, monedas no soportadas,
    incoherencias ni duplicados. No lee Django settings, raw ni campos de identidad.
    Empates se calculan antes de limitar; n es estricto y desempata por external_id.
    """
    if type(n) is not int:
        raise TypeError("El límite de ofertas debe ser un entero, no bool.")
    if n <= 0:
        raise ValueError("El límite de ofertas debe ser positivo.")
    if tipo_cambio is not None and not isinstance(tipo_cambio, TipoCambio):
        raise TypeError("Se requiere TipoCambio con valor, origen y fecha de captura.")
    grupos = {}
    errores = {}
    empates = set()
    vistos = set()
    for resultado in resultados:
        if not isinstance(resultado, ProviderResult):
            raise TypeError("La comparación recibe ProviderResult.")
        code = resultado.provider_code
        if not isinstance(code, str) or not code.strip():
            raise ValueError("Cada resultado requiere un código de proveedor válido.")
        if code in vistos:
            raise ValueError("Resultado de proveedor duplicado; no se sobrescriben ofertas.")
        vistos.add(code)
        if not resultado.ok:
            if not isinstance(resultado.error, ProviderError):
                raise TypeError("El error del proveedor debe ser ProviderError.")
            if resultado.error.provider_code != code:
                raise ValueError("El error no corresponde al proveedor del resultado.")
            errores[code] = resultado.error
            continue
        puntuadas = []
        ids = set()
        for oferta in resultado.offers:
            if not isinstance(oferta, ProviderOffer):
                raise TypeError("Las ofertas deben respetar ProviderOffer.")
            if oferta.provider_code != code:
                raise ValueError("La oferta no corresponde al proveedor del resultado.")
            if oferta.external_id in ids:
                raise ValueError("ID de oferta duplicado dentro del proveedor; requiere revisión.")
            ids.add(oferta.external_id)
            puntuadas.append((_precio_comparable(oferta, tipo_cambio), oferta))
        puntuadas.sort(key=lambda par: (par[0], par[1].external_id))
        if not puntuadas:
            grupos[code] = ()
            continue
        mejor = puntuadas[0][0]
        if sum(precio == mejor for precio, _ in puntuadas) > 1:
            empates.add(code)
        grupos[code] = tuple(OfertaComparada(oferta, precio, precio == mejor)
                             for precio, oferta in puntuadas[:n])
    return ResultadoComparacion(grupos, errores, frozenset(empates), tipo_cambio)


def comparar_configurado(
    resultados: Iterable[ProviderResult],
    *,
    tipo_cambio: TipoCambio | None = None,
    n: int | None = None,
) -> ResultadoComparacion:
    """Entrada Django: lee OFERTAS_POR_PROVEEDOR solo si no se pasó n explícito."""
    if n is None:
        from django.conf import settings

        n = settings.OFERTAS_POR_PROVEEDOR
    return comparar(resultados, tipo_cambio=tipo_cambio, n=n)
