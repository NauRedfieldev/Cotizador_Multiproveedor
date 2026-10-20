"""M6: ranking por proveedor en MXN, sin ORM, red ni persistencia.

Stock 0 y None participan y permanecen distintos. Mejor precio no garantiza
disponibilidad ni equivalencia de productos. No se cuantiza antes de ordenar.
El consumidor valida qué ofertas son alternativas y conserva el contexto de FX.

Con un término de búsqueda, primero va lo que el título nombra (nivel_de_coincidencia):
el proveedor también devuelve accesorios y productos relacionados por etiquetas, y por
precio esos quedaban arriba (p. ej. «monitor» → soportes de pared).
"""

import re
import unicodedata
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

DEFAULT_OFERTAS_POR_PROVEEDOR = 10
# Niveles de coincidencia con la búsqueda: menor es mejor.
NIVEL_SIN_COINCIDENCIA = 3
_PALABRAS_VACIAS = frozenset({
    "a", "al", "con", "de", "del", "el", "en", "la", "las", "los", "para", "por", "sin", "un", "una", "y",
})


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
    nivel: int = 0  # coincidencia con la búsqueda; 0 sin término de búsqueda


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


def _palabras(texto: str | None) -> list[str]:
    """Minúsculas, sin acentos y solo letras/dígitos: «Cámara IP-4MP» → camara, ip, 4mp."""
    texto = unicodedata.normalize("NFKD", texto or "").lower()
    return re.findall(r"[a-z0-9]+", "".join(c for c in texto if not unicodedata.combining(c)))


def palabras_de_busqueda(termino: str | None) -> tuple[str, ...]:
    return tuple(p for p in _palabras(termino) if p not in _PALABRAS_VACIAS)


def _misma_palabra(buscada: str, palabra: str) -> bool:
    """Palabra completa con plural: «monitor» acepta «monitores» pero no «monitoreable»."""
    return palabra in (buscada, buscada + "s", buscada + "es") or buscada in (palabra + "s", palabra + "es")


def _contiene(buscadas: tuple[str, ...], palabras: list[str]) -> bool:
    return all(any(_misma_palabra(b, p) for p in palabras) for b in buscadas)


def nivel_de_coincidencia(oferta: ProviderOffer, buscadas: tuple[str, ...]) -> int:
    """0: el título empieza por lo buscado (o es su modelo); 1: lo nombra en su primera parte;
    2: lo nombra después o en marca/modelo; 3: no lo nombra.

    La primera parte del título (hasta « / ») dice qué es el producto: «Monitor LED 32"» frente
    a «Soporte de Pared para Monitor» o «Kit de Videoportero / Monitor 7"».
    """
    if not buscadas:
        return 0
    modelo = _palabras(oferta.model)
    if modelo and "".join(modelo) == "".join(buscadas):
        return 0
    primera = _palabras(oferta.name.split(" / ", 1)[0])
    if _contiene(buscadas, primera):
        return 0 if any(_misma_palabra(b, primera[0]) for b in buscadas) else 1
    if _contiene(buscadas, _palabras(oferta.name) + _palabras(oferta.brand) + modelo):
        return 2
    return NIVEL_SIN_COINCIDENCIA


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
    termino: str | None = None,
) -> ResultadoComparacion:
    """Núcleo puro: mejores precios por proveedor, con todos los estados de stock.

    Errores M2 se conservan sin usar sus ofertas. Un error de validación del
    comparador sí se lanza: no ocultar USD sin convertir, monedas no soportadas,
    incoherencias ni duplicados. No lee Django settings ni raw.
    Sin término: las n más baratas; empates antes de limitar; desempata por external_id.
    Con término: las n con mejor nivel de coincidencia, en el orden del proveedor (su
    relevancia), y luego por nivel y precio. «Mejor precio» y empates se calculan entre
    las elegidas del mejor nivel presente: al buscar «monitor» compiten los monitores, no un
    soporte «para monitor» más barato.
    """
    if type(n) is not int:
        raise TypeError("El límite de ofertas debe ser un entero, no bool.")
    if n <= 0:
        raise ValueError("El límite de ofertas debe ser positivo.")
    if tipo_cambio is not None and not isinstance(tipo_cambio, TipoCambio):
        raise TypeError("Se requiere TipoCambio con valor, origen y fecha de captura.")
    buscadas = palabras_de_busqueda(termino)
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
        if not puntuadas:
            grupos[code] = ()
            continue
        # (nivel, posición del proveedor, precio, oferta)
        filas = [(nivel_de_coincidencia(oferta, buscadas), i, precio, oferta)
                 for i, (precio, oferta) in enumerate(puntuadas)]
        if buscadas:
            filas = sorted(filas, key=lambda f: (f[0], f[1]))[:n]
            base = [f for f in filas if f[0] == filas[0][0]]
        else:
            filas.sort(key=lambda f: (f[2], f[3].external_id))
            base = filas
        mejor = min(f[2] for f in base)
        if sum(f[2] == mejor for f in base) > 1:
            empates.add(code)
        candidatas = {f[1] for f in base}
        filas = sorted(filas[:n], key=lambda f: (f[0], f[2], f[3].external_id))
        grupos[code] = tuple(OfertaComparada(oferta, precio, precio == mejor and i in candidatas, nivel)
                             for nivel, i, precio, oferta in filas)
    return ResultadoComparacion(grupos, errores, frozenset(empates), tipo_cambio)


def comparar_configurado(
    resultados: Iterable[ProviderResult],
    *,
    tipo_cambio: TipoCambio | None = None,
    n: int | None = None,
    termino: str | None = None,
) -> ResultadoComparacion:
    """Entrada Django: lee OFERTAS_POR_PROVEEDOR solo si no se pasó n explícito."""
    if n is None:
        from django.conf import settings

        n = settings.OFERTAS_POR_PROVEEDOR
    return comparar(resultados, tipo_cambio=tipo_cambio, n=n, termino=termino)
