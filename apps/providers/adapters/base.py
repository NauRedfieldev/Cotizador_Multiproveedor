"""Contrato de los adaptadores de proveedores y funciones auxiliares (SDD M2 §4.3)."""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar, Mapping

import httpx

from apps.providers.contracts import ProductQuery, ProviderOffer
from apps.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderResponseError,
)

if TYPE_CHECKING:
    from apps.providers.models import Provider


@dataclass(frozen=True, slots=True)
class NewToken:
    access_token: str = field(repr=False)  # nunca aparece en repr ni en logs
    expires_in: int  # segundos de vida

    def __post_init__(self) -> None:
        if not isinstance(self.access_token, str) or not self.access_token:
            raise ValueError("access_token no puede estar vacío.")
        if isinstance(self.expires_in, bool) or not isinstance(self.expires_in, int) or self.expires_in <= 0:
            raise ValueError("expires_in debe ser un entero positivo.")


class ProviderAdapter(ABC):
    code: ClassVar[str]  # igual a Provider.code
    required_credentials: ClassVar[tuple[str, ...]] = ()  # p. ej. ("CLIENT_ID", "CLIENT_SECRET")
    uses_token: ClassVar[bool] = False  # True si la API usa un token OAuth2

    def __init__(self, provider: Provider, credentials: Mapping[str, str]) -> None:
        self.provider = provider
        self.credentials = credentials

    @abstractmethod
    async def fetch(self, query: ProductQuery, client: httpx.AsyncClient) -> list[Mapping[str, Any]]:
        """Pide los productos a la API y devuelve los elementos crudos, sin convertir.

        Usa raise_for_status y parse_json. Si uses_token, obtiene el token con
        await self.token(client).
        """

    @abstractmethod
    def parse_offer(self, item: Mapping[str, Any]) -> ProviderOffer:
        """Convierte un elemento crudo en oferta (provider_code = self.code, raw = item).

        Si el elemento es inválido lanza ProviderResponseError, ValueError, TypeError o
        KeyError, y el núcleo lo descarta.
        """

    async def request_token(self, client: httpx.AsyncClient) -> NewToken:
        """Solo si uses_token. Debe traducir el rechazo de credenciales del endpoint de
        tokens (400/401) a ProviderAuthError."""
        raise ProviderConfigError(self.code, "El adaptador no implementa request_token.")

    async def token(self, client: httpx.AsyncClient) -> str:
        """Token vigente: se reutiliza el de ProviderToken o se renueva."""
        from apps.providers.tokens import get_token

        return await get_token(self.provider, lambda: self.request_token(client))


def get_credentials(code: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Lee PROVIDER_<CODE>_<CLAVE> del entorno. Los guiones del code pasan a guion bajo."""
    prefijo = f"PROVIDER_{code.upper().replace('-', '_')}_"
    nombres = {key: prefijo + key for key in keys}
    faltan = [nombre for nombre in nombres.values() if not os.environ.get(nombre)]
    if faltan:
        # El mensaje lleva los nombres, nunca los valores.
        raise ProviderConfigError(code, "Faltan credenciales: " + ", ".join(faltan))
    return {key: os.environ[nombre] for key, nombre in nombres.items()}


def _rechazar_constante(constante: str) -> Any:
    # NaN e Infinity no son JSON estándar y llegarían como float.
    raise ValueError(constante)


def parse_json(code: str, response: httpx.Response) -> Any:
    try:
        return json.loads(response.text, parse_float=Decimal, parse_constant=_rechazar_constante)
    except ValueError:
        raise ProviderResponseError(code, "Respuesta no es JSON válido.") from None


def parse_price(code: str, value: object) -> Decimal:
    """Acepta Decimal, int (no bool) y str numérica; rechaza float, bool, None, NaN/Infinity
    y negativos."""
    invalido = ProviderResponseError(code, f"Precio inválido: {value!r}")
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise invalido
    try:
        precio = value if isinstance(value, Decimal) else Decimal(value)
    except InvalidOperation:
        raise invalido from None
    if not precio.is_finite() or precio < 0:
        raise invalido
    return precio


def raise_for_status(code: str, response: httpx.Response) -> None:
    status = response.status_code
    if status in (401, 403):
        raise ProviderAuthError(code, f"Autenticación rechazada (HTTP {status}).", status=status)
    if status == 429:
        segundos = response.headers.get("Retry-After", "").strip()
        raise ProviderRateLimitError(
            code,
            "Límite de peticiones superado (HTTP 429).",
            retry_after=int(segundos) if segundos.isdigit() else None,
        )
    if status >= 400:
        raise ProviderResponseError(code, f"HTTP {status} del proveedor.")
