"""Contratos de datos entre el núcleo de proveedores y quien lo consume (SDD M2 §4.1)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from apps.providers.errors import ProviderError

DEFAULT_LIMIT = 60
MAX_LIMIT = 1000  # SYSCOM admite limit 10–1000; cada adaptador lo traduce a su API

_CURRENCY = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True, slots=True)
class ProductQuery:
    term: str = ""
    sku: str | None = None
    limit: int = DEFAULT_LIMIT

    def __post_init__(self) -> None:
        object.__setattr__(self, "term", self.term.strip())
        object.__setattr__(self, "sku", (self.sku or "").strip() or None)
        if not self.term and self.sku is None:
            raise ValueError("La consulta requiere 'term' o 'sku'.")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_LIMIT
        ):
            raise ValueError("limit debe estar entre 1 y 1000.")


@dataclass(frozen=True, slots=True)
class ProviderOffer:
    provider_code: str
    external_id: str  # id del producto en el proveedor (mismo criterio que aupsert)
    name: str
    price: Decimal  # nunca float
    currency: str  # ISO 4217 en mayúsculas: "USD", "MXN"
    brand: str | None = None  # brand, model y mpn: datos para el emparejamiento de catalog
    model: str | None = None
    mpn: str | None = None
    stock: int | None = None  # None = el proveedor no informa existencias
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.external_id, str) or not self.external_id.strip():
            raise ValueError("external_id no puede estar vacío.")
        if not isinstance(self.price, Decimal):
            raise TypeError(f"price debe ser Decimal, no {type(self.price).__name__}.")
        if not self.price.is_finite() or self.price < 0:
            raise ValueError("price debe ser un Decimal finito y >= 0.")
        if not isinstance(self.currency, str) or not _CURRENCY.fullmatch(self.currency):
            raise ValueError("currency debe ser un código ISO 4217 en mayúsculas.")
        if self.stock is not None and self.stock < 0:
            raise ValueError("stock no puede ser negativo.")


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider_code: str
    offers: tuple[ProviderOffer, ...] = ()
    error: ProviderError | None = None
    elapsed_ms: int = 0
    discarded: int = 0  # ofertas inválidas descartadas

    @property
    def ok(self) -> bool:
        return self.error is None
