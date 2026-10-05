"""Errores de los proveedores (SDD M2 §4.2).

search_all no los lanza: los devuelve dentro de cada ProviderResult. Sus mensajes nunca
incluyen tokens, credenciales ni payloads.
"""


class ProviderError(Exception):
    def __init__(self, provider_code: str, message: str) -> None:
        super().__init__(provider_code, message)
        self.provider_code = provider_code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.provider_code}] {self.message}"


class ProviderTimeout(ProviderError):
    """Se superó Provider.timeout_ms."""


class ProviderAuthError(ProviderError):
    """Rechazo de autenticación. Con 401 el núcleo renueva el token y reintenta una vez."""

    def __init__(self, provider_code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(provider_code, message)
        self.status = status


class ProviderRateLimitError(ProviderError):
    """HTTP 429. retry_after son los segundos del header Retry-After, si es un entero."""

    def __init__(self, provider_code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(provider_code, message)
        self.retry_after = retry_after


class ProviderResponseError(ProviderError):
    """Otros 4xx/5xx, JSON inválido, error de conexión o datos inválidos."""


class ProviderConfigError(ProviderError):
    """Sin adaptador, sin credenciales o adaptador incompleto."""
