"""Token OAuth2 de cada proveedor, guardado en ProviderToken (SDD M2 §4.4 y §6.3).

El token nunca aparece en logs ni en mensajes de error.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from django.utils import timezone

from apps.providers.models import Provider, ProviderToken

if TYPE_CHECKING:
    from apps.providers.adapters.base import NewToken

RENEWAL_MARGIN = timedelta(minutes=5)

# Un Lock por (bucle de eventos, proveedor): un asyncio.Lock no debe compartirse entre bucles
# (las pruebas con async_to_sync crean uno por llamada). Entre procesos gana la última escritura.
_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]] = WeakKeyDictionary()


def _lock(provider: Provider) -> asyncio.Lock:
    por_proveedor = _locks.setdefault(asyncio.get_running_loop(), {})
    return por_proveedor.setdefault(provider.pk, asyncio.Lock())


async def _vigente(provider: Provider) -> str | None:
    token = await ProviderToken.objects.filter(provider=provider).afirst()
    if token is not None and token.expires_at > timezone.now() + RENEWAL_MARGIN:
        return token.access_token
    return None


async def get_token(provider: Provider, request: Callable[[], Awaitable[NewToken]]) -> str:
    """Devuelve el token vigente o pide uno nuevo con request() y lo guarda.

    Si request() falla, el error se propaga tal cual y no se guarda nada.
    """
    if token := await _vigente(provider):
        return token
    async with _lock(provider):
        # Otra tarea pudo renovarlo mientras esperábamos el Lock.
        if token := await _vigente(provider):
            return token
        nuevo = await request()
        ahora = timezone.now()
        await ProviderToken.objects.aupdate_or_create(
            provider=provider,
            defaults={
                "access_token": nuevo.access_token,
                "expires_at": ahora + timedelta(seconds=nuevo.expires_in),
                "obtained_at": ahora,
            },
        )
        return nuevo.access_token


async def invalidate_token(provider: Provider) -> None:
    """Marca el token como caducado (la fila se conserva); la siguiente llamada lo renueva."""
    await ProviderToken.objects.filter(provider=provider).aupdate(expires_at=timezone.now())
