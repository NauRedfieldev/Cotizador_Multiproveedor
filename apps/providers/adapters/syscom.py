"""Adaptador de la API de SYSCOM (SDD M3, docs/16-sdd-m3-syscom.md).

Solo aporta lo específico de SYSCOM. El núcleo de M2 ya resuelve el paralelismo, los timeouts,
el token (guardado en ProviderToken y reutilizado), el reintento ante un 401 y el descarte de
ofertas inválidas.
"""
from __future__ import annotations

from typing import Any, Mapping

import httpx

from apps.providers.adapters.base import (
    NewToken,
    ProviderAdapter,
    parse_json,
    parse_price,
    raise_for_status,
)
from apps.providers.contracts import ProductQuery, ProviderOffer
from apps.providers.errors import ProviderAuthError, ProviderResponseError
from apps.providers.registry import register

# Siempre los mismos parámetros (D2): dólares y sin IVA; quotes convierte y aplica el IVA.
PARAMETROS_FIJOS = {"moneda": "usd", "iva": "0"}
MONEDA = "USD"
# Límites de GET /productos según la documentación de SYSCOM (D5).
LIMITE_MINIMO = 10
MAX_PALABRAS = 10
MAX_CARACTERES = 120
# Campos confirmados con la API real el 2026-10-05 (docs/16 §4.3).
CAMPO_PRECIO = "precio_descuento"  # el precio de la cuenta de CCONOR (D12)
SIN_PRODUCTO = "product_not_available"  # 404 de ?modelo= cuando SYSCOM no vende ese modelo (D14)


@register
class SyscomAdapter(ProviderAdapter):
    code = "syscom"
    required_credentials = ("CLIENT_ID", "CLIENT_SECRET")
    uses_token = True

    @property
    def _base_url(self) -> str:
        return self.provider.base_url.rstrip("/")

    async def request_token(self, client: httpx.AsyncClient) -> NewToken:
        respuesta = await client.post(
            f"{self._base_url}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.credentials["CLIENT_ID"],
                "client_secret": self.credentials["CLIENT_SECRET"],
            },
        )
        if respuesta.status_code in (400, 401):
            # Sin status: el núcleo no reintenta y no se gasta otro token del límite por client_id/IP (D13).
            raise ProviderAuthError(
                self.code, f"SYSCOM rechazó las credenciales (HTTP {respuesta.status_code})."
            )
        raise_for_status(self.code, respuesta)
        datos = parse_json(self.code, respuesta)
        try:
            return NewToken(datos["access_token"], datos["expires_in"])
        except (KeyError, TypeError, ValueError):
            raise ProviderResponseError(self.code, "Respuesta de token inválida.") from None

    async def fetch(self, query: ProductQuery, client: httpx.AsyncClient) -> list[Mapping[str, Any]]:
        token = await self.token(client)
        if query.sku:
            params = {"modelo": query.sku}  # búsqueda exacta; manda sobre term (D4)
        else:
            params = {"busqueda": _recortar(query.term), "limit": max(query.limit, LIMITE_MINIMO)}
        respuesta = await client.get(
            f"{self._base_url}/productos",
            params={**params, **PARAMETROS_FIJOS},
            headers={"Authorization": f"Bearer {token}"},
        )
        if query.sku and self._producto_no_disponible(respuesta):
            return []
        raise_for_status(self.code, respuesta)
        datos = parse_json(self.code, respuesta)
        productos = self._producto(datos) if query.sku else self._productos(datos)
        return productos[: query.limit]

    def parse_offer(self, item: Mapping[str, Any]) -> ProviderOffer:
        titulo = item["titulo"]
        if not isinstance(titulo, str) or not titulo.strip():
            raise ValueError("Producto sin título.")
        precio = parse_price(self.code, item["precios"][CAMPO_PRECIO])
        if precio == 0:
            # En el comparador saldría como el más barato (D12).
            raise ValueError("Producto con precio 0.")
        modelo = _texto(item.get("modelo"))
        return ProviderOffer(
            provider_code=self.code,
            external_id=_identificador(item["producto_id"]),
            name=titulo.strip(),
            price=precio,
            currency=MONEDA,
            brand=_texto(item.get("marca")),
            model=modelo,
            mpn=modelo,  # el modelo de SYSCOM es el número de parte del fabricante (D11)
            stock=_existencias(item.get("total_existencia")),
            raw=item,
        )

    def _producto_no_disponible(self, respuesta: httpx.Response) -> bool:
        """El único 404 que no es error: SYSCOM no vende el modelo pedido."""
        if respuesta.status_code != 404:
            return False
        try:
            cuerpo = parse_json(self.code, respuesta)
        except ProviderResponseError:
            return False
        return isinstance(cuerpo, dict) and cuerpo.get("error") == SIN_PRODUCTO

    def _producto(self, datos: Any) -> list[Mapping[str, Any]]:
        """?modelo= devuelve un único producto como objeto."""
        if not isinstance(datos, dict):
            raise ProviderResponseError(self.code, "Respuesta de producto inválida.")
        return [datos]

    def _productos(self, datos: Any) -> list[Mapping[str, Any]]:
        """?busqueda= devuelve {"cantidad", "pagina", "paginas", "productos": [...], "todo"}."""
        productos = datos.get("productos") if isinstance(datos, dict) else None
        if not isinstance(productos, list):
            raise ProviderResponseError(self.code, "Respuesta sin lista de productos.")
        return productos


def _recortar(termino: str) -> str:
    """busqueda admite como mucho 10 palabras y 120 caracteres."""
    return " ".join(termino.split()[:MAX_PALABRAS])[:MAX_CARACTERES].rstrip()


def _identificador(valor: object) -> str:
    if isinstance(valor, bool) or not isinstance(valor, (str, int)):
        raise TypeError("producto_id inválido.")
    return str(valor)


def _texto(valor: object) -> str | None:
    """Texto sin espacios alrededor, o None si no es texto o queda vacío."""
    if not isinstance(valor, str):
        return None
    return valor.strip() or None


def _existencias(valor: object) -> int | None:
    """Entero >= 0 (no bool). Cualquier otra cosa es None y la oferta se conserva."""
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        return None
    return valor
