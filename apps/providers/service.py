"""Orquestación: consulta en paralelo a todos los proveedores activos (SDD M2 §4.6 y §6.2)."""
import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any

import httpx

from apps.providers.adapters.base import ProviderAdapter, get_credentials
from apps.providers.contracts import ProductQuery, ProviderOffer, ProviderResult
from apps.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderResponseError,
    ProviderTimeout,
)
from apps.providers.models import Provider
from apps.providers.registry import get_adapter_class
from apps.providers.tokens import invalidate_token

logger = logging.getLogger("apps.providers")

# Lo que parse_offer puede lanzar ante un elemento inválido: se descarta solo esa oferta.
_OFERTA_INVALIDA = (ProviderResponseError, ValueError, TypeError, KeyError)


async def search_all(
    query: ProductQuery,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[ProviderResult]:
    """Un ProviderResult por cada Provider activo, en orden de code.

    Nunca lanza ProviderError: los errores van dentro de cada resultado. Deja propagar
    asyncio.CancelledError. Solo escribe en la BD para guardar o invalidar tokens.
    """
    proveedores = [p async for p in Provider.objects.filter(active=True).order_by("code")]
    if not proveedores:
        return []
    # Sin timeout propio: manda el wait_for con Provider.timeout_ms (el de httpx, 5 s, cortaría antes).
    async with httpx.AsyncClient(transport=transport, timeout=None) as client:
        resultados = await asyncio.gather(
            *(_query_provider(p, query, client) for p in proveedores), return_exceptions=True
        )
    return [_as_result(p, r) for p, r in zip(proveedores, resultados)]


def _as_result(provider: Provider, resultado: object) -> ProviderResult:
    if isinstance(resultado, ProviderResult):
        return resultado
    if isinstance(resultado, asyncio.CancelledError):
        raise resultado
    nombre = type(resultado).__name__
    logger.error("Fallo inesperado en la orquestación de %s: %s", provider.code, nombre)
    return ProviderResult(
        provider.code, error=ProviderResponseError(provider.code, f"Error inesperado: {nombre}.")
    )


async def _query_provider(
    provider: Provider, query: ProductQuery, client: httpx.AsyncClient
) -> ProviderResult:
    inicio = time.perf_counter()
    ofertas: tuple[ProviderOffer, ...] = ()
    descartadas = 0
    error: ProviderError | None = None
    try:
        clase = get_adapter_class(provider.code)
        if clase is None:
            raise ProviderConfigError(provider.code, "No hay adaptador registrado.")
        adaptador = clase(provider, get_credentials(provider.code, clase.required_credentials))
        items = await asyncio.wait_for(
            _fetch_with_auth_retry(adaptador, query, client), timeout=provider.timeout_ms / 1000
        )
        ofertas, descartadas = _parse_offers(adaptador, items)
    except (TimeoutError, httpx.TimeoutException):
        error = ProviderTimeout(provider.code, f"Sin respuesta en {provider.timeout_ms} ms.")
    except ProviderError as exc:
        error = exc
    except httpx.HTTPError as exc:
        error = ProviderResponseError(provider.code, f"Error de conexión: {type(exc).__name__}.")
    except Exception as exc:
        logger.exception("Error inesperado consultando %s", provider.code)
        error = ProviderResponseError(provider.code, f"Error inesperado: {type(exc).__name__}.")
    transcurrido = round((time.perf_counter() - inicio) * 1000)
    return ProviderResult(provider.code, ofertas, error, transcurrido, descartadas)


async def _fetch_with_auth_retry(
    adaptador: ProviderAdapter, query: ProductQuery, client: httpx.AsyncClient
) -> Iterable[Mapping[str, Any]]:
    """Un 401 con token invalida el token y reintenta una sola vez, dentro del mismo timeout."""
    try:
        return await adaptador.fetch(query, client)
    except ProviderAuthError as exc:
        if exc.status != 401 or not adaptador.uses_token:
            raise
    await invalidate_token(adaptador.provider)
    return await adaptador.fetch(query, client)


def _parse_offers(
    adaptador: ProviderAdapter, items: Iterable[Mapping[str, Any]]
) -> tuple[tuple[ProviderOffer, ...], int]:
    """Convierte cada elemento; los inválidos se descartan y se cuentan (sin registrar su contenido)."""
    ofertas = []
    descartadas = 0
    for item in items:
        try:
            oferta = adaptador.parse_offer(item)
        except _OFERTA_INVALIDA as exc:
            descartadas += 1
            logger.warning("Oferta descartada de %s: %s", adaptador.code, type(exc).__name__)
            continue
        if oferta.provider_code != adaptador.code:
            descartadas += 1
            logger.warning("Oferta descartada de %s: provider_code ajeno.", adaptador.code)
            continue
        ofertas.append(oferta)
    return tuple(ofertas), descartadas
