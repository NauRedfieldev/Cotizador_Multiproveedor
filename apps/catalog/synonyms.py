"""Sinónimos de BD, por tokens completos y sin expansión recursiva.

La caché es local al proceso: otros procesos pueden tardar hasta 60 segundos
en ver una edición. Las lecturas dentro de transacciones no usan ni rellenan
la caché para no publicar datos que podrían revertirse.

save() y delete() (incluido QuerySet.delete()) invalidan mediante señales.
update(), bulk_create() y bulk_update() omiten save()/sus señales: quien los
utilice debe normalizar/validar los datos e invalidar explícitamente la caché
después del commit. Para uso async, llamar a await aexpandir(texto_norm).
"""

from threading import Lock
from time import monotonic

from asgiref.sync import sync_to_async
from django.db import connection

from .models import SinonimoRed

TTL_SINONIMOS = 60
_lock = Lock()
_sinonimos = None
_vence_en = 0.0


def invalidar_cache() -> None:
    """Descarta únicamente la copia de sinónimos de este proceso."""
    global _sinonimos, _vence_en
    with _lock:
        _sinonimos = None
        _vence_en = 0.0


def _leer_sinonimos() -> dict[str, str]:
    return dict(SinonimoRed.objects.values_list("abreviatura", "expansion"))


def _obtener_sinonimos() -> dict[str, str]:
    global _sinonimos, _vence_en
    if connection.in_atomic_block:
        return _leer_sinonimos()
    with _lock:
        if _sinonimos is None or monotonic() >= _vence_en:
            _sinonimos = _leer_sinonimos()
            _vence_en = monotonic() + TTL_SINONIMOS
        return _sinonimos


def expandir(texto_norm: str) -> str:
    """Sustituye tokens separados por espacios; no modifica tokens desconocidos."""
    partes = texto_norm.split()
    if not partes:
        return ""
    sinonimos = _obtener_sinonimos()
    return " ".join(sinonimos.get(token, token) for token in partes)


async def aexpandir(texto_norm: str) -> str:
    """Vía async: carga la BD fuera del event loop, en un hilo seguro para Django."""
    return await sync_to_async(expandir, thread_sensitive=True)(texto_norm)
