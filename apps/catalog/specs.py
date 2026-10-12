"""Guardas puras iniciales de redes; no garantizan equivalencia de productos.

Se reciben textos normalizados, ANTES de expandir sinónimos. Se conservan todas
las apariciones. Varias declaraciones incompatibles de un atributo exclusivo
impiden sugerir ese texto; uplinks puede describir varias clases simultáneas.
"""

import re
from collections.abc import Mapping

_UPLINKS = re.compile(r"(?<![A-Z0-9.])(\d+)\s*X\s*(\d+)G(?![A-Z0-9])")
_PUERTOS = re.compile(r"(?<![A-Z0-9.])(\d+)\s*(?:PUERTOS?|P)(?![A-Z0-9])")
_POE = re.compile(r"(?<![A-Z0-9])(?:(SIN|NO)\s+)?POE(\+*)(?![A-Z0-9+])")
_LICENCIA = re.compile(r"(?<![A-Z0-9])(LAN\s*(BASE|LITE)|IP\s*(BASE|SERVICES))(?![A-Z0-9])")
_RACK = re.compile(r"(?<![A-Z0-9./])(\d+(?:\.\d+)?)\s*U(?![A-Z0-9])")
_EXCLUSIVOS = {"puertos", "poe", "licencia", "rack"}


def specs(texto_norm: str) -> dict[str, frozenset[str]]:
    """Extrae valores explícitos; una ausencia no se convierte en un valor supuesto."""
    valores = {
        "uplinks": {f"{m[1]}X{m[2]}G" for m in _UPLINKS.finditer(texto_norm)},
        "puertos": {m[1] for m in _PUERTOS.finditer(texto_norm)},
        "poe": {("SIN_" if m[1] else "") + "POE" + m[2] for m in _POE.finditer(texto_norm)},
        "licencia": {re.sub(r"\s+", "", m[1]) for m in _LICENCIA.finditer(texto_norm)},
        "rack": {m[1] for m in _RACK.finditer(texto_norm)},
    }
    return {campo: frozenset(conjunto) for campo, conjunto in valores.items() if conjunto}


def compatibles(a: Mapping[str, frozenset[str]], b: Mapping[str, frozenset[str]]) -> bool:
    """Rechaza contradicciones explícitas; no exige que ambos textos sean completos."""
    if any(len(datos.get(campo, ())) > 1 for datos in (a, b) for campo in _EXCLUSIVOS):
        return False
    return all(a[campo] == b[campo] for campo in a.keys() & b.keys())
