"""Registro de adaptadores de proveedores por code (SDD M2 §4.5)."""
import re

from apps.providers.adapters.base import ProviderAdapter

_REGISTRY: dict[str, type[ProviderAdapter]] = {}
_CODE = re.compile(r"[a-z0-9_-]+")


def register(cls: type[ProviderAdapter]) -> type[ProviderAdapter]:
    """Decorador: registra el adaptador con su code (igual a Provider.code)."""
    code = getattr(cls, "code", None)
    if not code:
        raise ValueError(f"El adaptador {cls.__name__} no define code.")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ValueError(f"Código de adaptador inválido: {code!r}.")
    if code in _REGISTRY:
        raise ValueError(f"Adaptador duplicado para el proveedor '{code}'.")
    _REGISTRY[code] = cls
    return cls


def get_adapter_class(code: str) -> type[ProviderAdapter] | None:
    return _REGISTRY.get(code)


def registered_codes() -> list[str]:
    return sorted(_REGISTRY)
