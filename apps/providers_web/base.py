"""Base de los scrapers de proveedores sin API (SDD M9, docs/31-sdd-m9-exel-web.md).

Un scraper es un ProviderAdapter más: search_all lo consulta igual que a un adaptador de API. Si el
sitio exige sesión, el scraper la abre en request_token y la devuelve como token (las cookies, como
una cabecera Cookie), así que el núcleo la guarda en ProviderToken, la reutiliza y la renueva una
sola vez ante un ProviderAuthError(status=401).
"""
from __future__ import annotations

import httpx

from apps.providers.adapters.base import ProviderAdapter

# Las mismas cabeceras con las que se hizo el reconocimiento del sitio (docs/31 §10).
CABECERAS_NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9",
}
# Las que añade el navegador (jQuery) a las llamadas de la página a sus servicios JSON.
CABECERAS_AJAX = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


class WebScraperAdapter(ProviderAdapter):
    """Lo común a los scrapers: la URL de la tienda, las cabeceras de navegador y la cookie de sesión."""

    @property
    def _base_url(self) -> str:
        return self.provider.base_url.rstrip("/")

    def _cabeceras(self, cookie: str | None = None, *, ajax: bool = False) -> dict[str, str]:
        """La cookie va explícita en cada petición: el AsyncClient del núcleo es compartido."""
        cabeceras = {**CABECERAS_NAVEGADOR, **(CABECERAS_AJAX if ajax else {})}
        if cookie:
            cabeceras["Cookie"] = cookie
        return cabeceras


def unir_cookies(*respuestas: httpx.Response) -> str:
    """Las cookies que fijan las respuestas (Set-Cookie), como cabecera Cookie: "a=1; b=2".

    Si una cookie se repite, gana la última respuesta. Una cookie vaciada (sin valor) se quita.
    """
    cookies: dict[str, str] = {}
    for respuesta in respuestas:
        for cabecera in respuesta.headers.get_list("set-cookie"):
            nombre, _, resto = cabecera.partition("=")
            nombre, valor = nombre.strip(), resto.split(";", 1)[0].strip()
            if not nombre:
                continue
            if valor:
                cookies[nombre] = valor
            else:
                cookies.pop(nombre, None)
    return "; ".join(f"{nombre}={valor}" for nombre, valor in cookies.items())


def es_redireccion(respuesta: httpx.Response) -> bool:
    return 300 <= respuesta.status_code < 400
