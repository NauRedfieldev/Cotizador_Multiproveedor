"""Scraper de la tienda de Exel del Norte (SDD M9, docs/31-sdd-m9-exel-web.md).

Exel no tiene API. Su tienda (ASP.NET WebForms) exige sesión, y su propia página busca con dos
servicios JSON: buscarJSON.aspx (productos, con el precio de lista) y buscarJSONprice.aspx (el
precio de la cuenta de CCONOR). Estructura confirmada con el sitio real el 2026-10-27 (docs/31 §4).

Solo aporta lo específico de Exel. El núcleo de M2 resuelve el paralelismo, el timeout, la sesión
(guardada en ProviderToken como token y renovada una vez ante un 401) y el descarte de ofertas.
Cada búsqueda correcta se guarda SEGUNDOS_CACHE en la caché de Django (docs/32 §11).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any, Mapping

import httpx
from django.core.cache import cache

from apps.providers.adapters.base import NewToken, parse_json, parse_price, raise_for_status
from apps.providers.contracts import ProductQuery, ProviderOffer
from apps.providers.errors import ProviderAuthError, ProviderResponseError
from apps.providers.registry import register
from apps.providers_web.base import WebScraperAdapter, es_redireccion, unir_cookies
from apps.providers_web.extraccion import campos_de_formulario

# Precios en pesos y sin IVA (confirmado por el usuario en el portal de Exel, 2026-10-27).
MONEDA = "MXN"
# Sesión ASP.NET: 20 min sin actividad. El núcleo la renueva 5 min antes (tokens.RENEWAL_MARGIN).
VIDA_SESION = 20 * 60
# Formulario de acceso (POST /Acceso, WebForms): los ocultos del GET + usuario, contraseña y botón.
FORMULARIO = "Form1"
CAMPO_USUARIO = "ctl00$MainContent$txtUsuario"
CAMPO_CONTRASENA = "ctl00$MainContent$txtPassword"
BOTON_ENTRAR = ("ctl00$MainContent$L_ButtonLogin", "Iniciar Sesión")
# Los mismos valores que pone la página al buscar (W1); siempre iguales.
PARAMETROS_FIJOS = {
    "borrarExistencia": "0",
    "ordenarPor": "orden inicial",
    "id_categoria": "0",
    "id_subcategoria": "0",
    "id_marca": "",
    "pruebas": "false",
}
MAX_CARACTERES = 100  # maxlength del buscador de la tienda
# Exel tarda ~6 s por búsqueda (búsqueda + precios, medido el 2026-10-28, docs/32 §11): una búsqueda
# repetida se sirve de la caché. La cuenta de CCONOR es una sola, así que vale para todos los asesores.
SEGUNDOS_CACHE = 5 * 60
# buscarJSONprice tarda ~45 ms por id y Exel atiende lotes a la vez sin penalizar (medido el
# 2026-10-28): 60 ids en 3 lotes paralelos ≈ 0,9 s en vez de 2,7 s en una sola petición.
TAMANO_LOTE = 20
# Lo que responde un servicio de la tienda sin sesión (según Scripts/buscar.js).
SIN_SESION = ("sin_usuario", "sin_cliente", "logout")
# W9: raw solo lleva estos campos; nunca los datos del agente de Exel ni la localidad de CCONOR.
CAMPOS_PRODUCTO = (
    "id_producto", "descripcion", "Articulo", "Marca", "id_marca", "codigo_proveedor", "Categoria",
    "SubCategoria", "Familia", "id_unidad", "Existencia", "ExistenciaMX", "ExistenciaNacional",
    "TieneExistencia", "tieneFoto", "es_nuevo", "esBundle", "es_backorder",
)
CAMPOS_PRECIO = ("precio", "precio_lista", "precio_normal", "origen", "vigencia")
# Foto y ficha: formato visto en las tarjetas del menú de la tienda (docs/31 §4).
SITIO_TIENDA = "https://www.exel.com.mx/xlstore"
FOTOS = "https://contenidos.exel.com.mx/imgProducto/"
_ID_PRODUCTO = re.compile(r"[A-Za-z0-9._-]{1,60}")


@register
class ExelScraper(WebScraperAdapter):
    code = "exel"
    required_credentials = ("USUARIO", "CONTRASENA")
    uses_token = True  # el «token» es la cabecera Cookie de la sesión (W3, W8)

    async def request_token(self, client: httpx.AsyncClient) -> NewToken:
        url = f"{self._base_url}/Acceso"
        formulario = await client.get(url, headers=self._cabeceras())
        self._rechazo_del_acceso(formulario)
        raise_for_status(self.code, formulario)
        campos = campos_de_formulario(formulario.text, FORMULARIO)
        if not campos or CAMPO_USUARIO not in campos or CAMPO_CONTRASENA not in campos:
            raise ProviderResponseError(self.code, "La estructura del sitio cambió (formulario de acceso).")
        cookie = unir_cookies(formulario)
        datos = {
            **campos,
            CAMPO_USUARIO: self.credentials["USUARIO"],
            CAMPO_CONTRASENA: self.credentials["CONTRASENA"],
            BOTON_ENTRAR[0]: BOTON_ENTRAR[1],
        }
        cabeceras = {**self._cabeceras(cookie), "Referer": url}
        respuesta = await client.post(url, data=datos, headers=cabeceras)
        if respuesta.status_code == 200 or (
            es_redireccion(respuesta) and "/acceso" in respuesta.headers.get("location", "").lower()
        ):
            # 200 = la misma página con «Usuario y/o contraseña incorrectos» (docs/31 §10).
            raise ProviderAuthError(self.code, "Exel rechazó el inicio de sesión.")
        self._rechazo_del_acceso(respuesta)
        raise_for_status(self.code, respuesta)
        sesion = unir_cookies(formulario, respuesta)
        if not es_redireccion(respuesta) or not sesion:
            raise ProviderResponseError(self.code, "Respuesta de inicio de sesión inesperada.")
        return NewToken(sesion, VIDA_SESION)

    async def fetch(self, query: ProductQuery, client: httpx.AsyncClient) -> list[Mapping[str, Any]]:
        clave = _clave_cache(query)
        if (guardado := await cache.aget(clave)) is not None:
            return guardado  # ni sesión ni peticiones
        cookie = await self.token(client)
        respuesta = await client.get(
            f"{self._base_url}/Productos/buscarJSON.aspx",
            params={"buscar": _recortar(query.sku or query.term), **PARAMETROS_FIJOS},
            headers=self._cabeceras(cookie, ajax=True),
        )
        datos = self._json(respuesta)
        productos = datos.get("Table") if isinstance(datos, dict) else None
        if not isinstance(productos, list):
            raise ProviderResponseError(self.code, "La estructura del sitio cambió (búsqueda).")
        if query.sku:
            # No hay búsqueda exacta: Exel devuelve «similares»; solo vale el mismo código (W11).
            productos = [p for p in productos if _mismo_codigo(p, query.sku)]
        productos = productos[: query.limit]
        precios = await self._precios(productos, cookie, client)  # sin productos, no pide nada
        items = [_crudo(p, precios) for p in productos]
        await cache.aset(clave, items, SEGUNDOS_CACHE)  # solo una respuesta correcta; un error no llega aquí
        return items

    def parse_offer(self, item: Mapping[str, Any]) -> ProviderOffer:
        descripcion = item["descripcion"]
        if not isinstance(descripcion, str) or not descripcion.strip():
            raise ValueError("Producto sin descripción.")
        # El precio de CCONOR, no el de lista que trae la búsqueda (W10).
        precio = parse_price(self.code, item["precios"]["precio"])
        if precio == 0:
            raise ValueError("Producto con precio 0.")  # saldría como el más barato
        id_producto = _identificador(item["id_producto"])
        codigo = _texto(item.get("codigo_proveedor"))
        return ProviderOffer(
            provider_code=self.code,
            external_id=id_producto,
            name=descripcion.strip(),
            price=precio,
            currency=MONEDA,
            brand=_texto(item.get("Marca")),
            model=codigo,
            mpn=codigo,  # el código de Exel es el SKU del fabricante (confirmado por el usuario)
            stock=_existencias(item.get("ExistenciaNacional")),
            raw=item,
            image_url=_foto(id_producto, item.get("tieneFoto")),
            product_url=_ficha(id_producto),
        )

    async def _precios(
        self, productos: list[Any], cookie: str, client: httpx.AsyncClient
    ) -> dict[str, dict[str, Any]]:
        """Precio de CCONOR de cada producto, en lotes de TAMANO_LOTE ids pedidos a la vez (docs/32 §11).

        Sin ids no hay lotes ni peticiones. Se esperan todos los lotes y luego se lanza el primer error,
        para no dejar peticiones sueltas si el núcleo reintenta.
        """
        ids = [p["id_producto"] for p in productos if isinstance(p, dict) and _id_valido(p.get("id_producto"))]
        lotes = [ids[i : i + TAMANO_LOTE] for i in range(0, len(ids), TAMANO_LOTE)]
        respuestas = await asyncio.gather(
            *(self._lote_de_precios(lote, cookie, client) for lote in lotes), return_exceptions=True
        )
        precios: dict[str, dict[str, Any]] = {}
        for respuesta in respuestas:
            if isinstance(respuesta, BaseException):
                raise respuesta
            precios.update(respuesta)
        return precios

    async def _lote_de_precios(
        self, ids: list[str], cookie: str, client: httpx.AsyncClient
    ) -> dict[str, dict[str, Any]]:
        respuesta = await client.get(
            f"{self._base_url}/Productos/buscarJSONprice.aspx",
            params={"listaproductos": "|".join(ids)},
            headers=self._cabeceras(cookie, ajax=True),
        )
        datos = self._json(respuesta)
        if not isinstance(datos, list):
            raise ProviderResponseError(self.code, "La estructura del sitio cambió (precios).")
        return {
            fila["id_producto"]: {campo: fila[campo] for campo in CAMPOS_PRECIO if campo in fila}
            for fila in datos
            if isinstance(fila, dict) and isinstance(fila.get("id_producto"), str)
        }

    def _json(self, respuesta: httpx.Response) -> Any:
        """Los servicios responden JSON anunciado como text/html (docs/31 §4)."""
        if _sesion_caducada(respuesta):
            # status=401: el núcleo invalida la sesión, vuelve a entrar y reintenta una vez.
            raise ProviderAuthError(self.code, "La sesión de Exel caducó.", status=401)
        raise_for_status(self.code, respuesta)
        return parse_json(self.code, respuesta)

    def _rechazo_del_acceso(self, respuesta: httpx.Response) -> None:
        """Un 400/401 del acceso va sin status: el núcleo no reintenta y no repite el login."""
        if respuesta.status_code in (400, 401):
            raise ProviderAuthError(
                self.code, f"Exel rechazó el inicio de sesión (HTTP {respuesta.status_code})."
            )


def _sesion_caducada(respuesta: httpx.Response) -> bool:
    """Sin sesión, la tienda redirige a /Acceso; un servicio puede responder sin_usuario/logout."""
    if es_redireccion(respuesta):
        return True
    if respuesta.status_code != 200:
        return False
    cuerpo = respuesta.text.lstrip()
    if cuerpo.startswith(("{", "[")):
        return False
    minusculas = cuerpo.lower()
    return any(marca in minusculas for marca in SIN_SESION) or "maincontent_txtpassword" in minusculas


def _crudo(producto: Any, precios: Mapping[str, Mapping[str, Any]]) -> Any:
    """El producto con solo los campos permitidos (W9) y, si lo hay, su precio de CCONOR."""
    if not isinstance(producto, dict):
        return producto  # parse_offer lo rechaza y el núcleo lo cuenta como descartado
    crudo = {campo: producto[campo] for campo in CAMPOS_PRODUCTO if campo in producto}
    precio = precios.get(producto.get("id_producto")) if _id_valido(producto.get("id_producto")) else None
    if precio is not None:
        crudo["precios"] = dict(precio)
    return crudo


def _clave_cache(query: ProductQuery) -> str:
    """Lo que decide el resultado: el sku (filtra), el texto enviado y limit. Sin cookie ni credenciales."""
    busqueda = [query.sku or None, None if query.sku else _recortar(query.term), query.limit]
    return "exel:busqueda:" + hashlib.sha256(json.dumps(busqueda).encode()).hexdigest()


def _mismo_codigo(producto: Any, sku: str) -> bool:
    codigo = _texto(producto.get("codigo_proveedor")) if isinstance(producto, dict) else None
    return codigo is not None and codigo.casefold() == sku.casefold()


def _recortar(texto: str) -> str:
    """El buscador admite como mucho 100 caracteres."""
    return " ".join(texto.split())[:MAX_CARACTERES].rstrip()


def _id_valido(valor: object) -> bool:
    return isinstance(valor, str) and bool(_ID_PRODUCTO.fullmatch(valor))


def _foto(id_producto: str, tiene_foto: object) -> str | None:
    if tiene_foto is True or tiene_foto != 1 or not _id_valido(id_producto):
        return None
    return f"{FOTOS}{id_producto}.png"


def _ficha(id_producto: str) -> str | None:
    """La ficha de la tienda (pide la sesión de Exel en el navegador del asesor)."""
    return f"{SITIO_TIENDA}/Productos/Detalle/{id_producto}" if _id_valido(id_producto) else None


def _identificador(valor: object) -> str:
    """Texto sin espacios alrededor; vacío lo rechaza ProviderOffer (ValueError)."""
    if not isinstance(valor, str):
        raise TypeError("id_producto inválido.")
    return valor.strip()


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
