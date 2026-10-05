"""Normalización pura de descripciones y claves; no realiza matching."""

import re
import unicodedata


def normalizar(texto: str) -> str:
    """Conserva los discriminantes +, /, . y - de las descripciones."""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.upper()
    texto = re.sub(r"[^A-Z0-9+/. -]", " ", texto)
    return " ".join(texto.split())


def normalizar_clave(clave: str) -> str:
    """Elimina separadores de identificadores exactos (MPN, SKU o modelo)."""
    return re.sub(r"[^A-Z0-9]", "", clave.upper())
