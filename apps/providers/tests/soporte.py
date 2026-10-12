"""Utilidades compartidas por las pruebas de apps.providers.

El runner de Django no carga conftest.py, así que lo común vive aquí.
"""
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone as dt_timezone
from unittest import mock

import httpx
from asgiref.sync import async_to_sync
from django.db import connection, connections

from apps.providers import registry
from apps.providers.adapters.base import (
    NewToken,
    ProviderAdapter,
    parse_json,
    parse_price,
    raise_for_status,
)
from apps.providers.contracts import ProviderOffer
from apps.providers.errors import ProviderAuthError
from apps.providers.models import Provider

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
T1 = datetime(2026, 1, 1, 12, 5, tzinfo=dt_timezone.utc)
T2 = datetime(2026, 1, 1, 12, 10, tzinfo=dt_timezone.utc)

PAYLOAD = {"precio": 100, "sku": "X1"}
PAYLOAD_NUEVO = {"precio": 120, "sku": "X1"}
# Calculados a mano con la fórmula documentada, sin pasar por compute_content_hash:
# sha256('{"precio":100,"sku":"X1"}') y sha256('{"precio":120,"sku":"X1"}').
HASH_PAYLOAD = "92db148659631470bf9e770d79e2a6ef226888d0d795973f96a51b58cb5326c1"
HASH_PAYLOAD_NUEVO = "7161fdf906838baeee3f1a038a42ea61e531a2429330f1fce014788363c05f5e"

CENTINELA_TOKEN = "tok-centinela-qa-7f3a9c-no-debe-aparecer"


def hash_independiente(payload):
    """Fórmula documentada del hash, calculada sin pasar por el código bajo prueba."""
    texto = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(texto.encode()).hexdigest()


def crear_proveedor(code="demo"):
    return Provider.objects.create(
        code=code, name=code.title(), base_url=f"https://{code}.example.com"
    )


class RelojControlado:
    """Sustituye django.utils.timezone.now por un reloj que solo avanza cuando el test lo pide."""

    def __init__(self, inicio):
        self.ahora = inicio
        self._parche = mock.patch("django.utils.timezone.now", new=lambda: self.ahora)

    def __enter__(self):
        self._parche.start()
        return self

    def __exit__(self, *excepcion):
        self._parche.stop()


def exigir_bd_de_pruebas():
    """Red de seguridad: estas pruebas nunca deben escribir en la BD de desarrollo."""
    nombre = connection.settings_dict["NAME"]
    if not nombre.startswith("test_"):
        raise RuntimeError(f"Se esperaba la BD de pruebas y la conexión apunta a {nombre!r}")


def ejecutar_en_hilos(funcion_async, argumentos_por_hilo):
    """Ejecuta funcion_async(*args) en un hilo real por cada tupla de argumentos.

    Con asyncio.gather el ORM async ejecuta las consultas una tras otra en un solo hilo,
    así que solo se intercalan entre awaits. Aquí cada hilo tiene su propia conexión a
    PostgreSQL, todos arrancan a la vez (Barrier) y las transacciones compiten de verdad.
    Devuelve el resultado o la excepción de cada hilo.
    """
    exigir_bd_de_pruebas()
    barrera = threading.Barrier(len(argumentos_por_hilo))
    resultados = [None] * len(argumentos_por_hilo)

    def trabajo(indice, args):
        try:
            barrera.wait()
            resultados[indice] = async_to_sync(funcion_async)(*args)
        except Exception as exc:
            resultados[indice] = exc
        finally:
            connections.close_all()

    hilos = [
        threading.Thread(target=trabajo, args=(i, args))
        for i, args in enumerate(argumentos_por_hilo)
    ]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    return resultados


# --- M2: adaptadores y API falsos para probar el contrato y la orquestación ------------


class AdaptadorDePrueba(ProviderAdapter):
    """Adaptador de una API JSON ficticia que usa las funciones auxiliares reales.

    GET  {base_url}/productos -> {"productos": [{"id", "nombre", "precio", ...}]}
    POST {base_url}/token     -> {"access_token", "expires_in"}   (solo si uses_token)
    """

    code = "prueba"

    async def fetch(self, query, client):
        cabeceras = {}
        if self.uses_token:
            cabeceras["Authorization"] = f"Bearer {await self.token(client)}"
        respuesta = await client.get(
            f"{self.provider.base_url}/productos",
            params={"q": query.term, "limit": query.limit},
            headers=cabeceras,
        )
        raise_for_status(self.code, respuesta)
        return parse_json(self.code, respuesta)["productos"]

    def parse_offer(self, item):
        return ProviderOffer(
            provider_code=self.code,
            external_id=str(item["id"]),
            name=item["nombre"],
            price=parse_price(self.code, item["precio"]),
            currency=item.get("moneda", "USD"),
            brand=item.get("marca"),
            model=item.get("modelo"),
            stock=item.get("existencia"),
            raw=item,
        )

    async def request_token(self, client):
        respuesta = await client.post(f"{self.provider.base_url}/token", data=dict(self.credentials))
        if respuesta.status_code in (400, 401):
            raise ProviderAuthError(
                self.code, "El endpoint de tokens rechazó las credenciales.", status=respuesta.status_code
            )
        raise_for_status(self.code, respuesta)
        datos = parse_json(self.code, respuesta)
        return NewToken(datos["access_token"], int(datos["expires_in"]))


def crear_adaptador(code, base=AdaptadorDePrueba, **atributos):
    """Subclase de `base` con el code indicado y atributos o métodos sustituidos."""
    return type(f"Adaptador_{code.replace('-', '_')}", (base,), {"code": code, **atributos})


@contextmanager
def adaptadores_registrados(*clases):
    """Registra adaptadores solo durante la prueba; el registro real se restaura al salir."""
    with mock.patch.dict(registry._REGISTRY, {}, clear=True):
        for clase in clases:
            registry.register(clase)
        yield


def respuesta_json(datos, status=200, cabeceras=None):
    return httpx.Response(status, json=datos, headers=cabeceras or {})


def productos(*items):
    return respuesta_json({"productos": list(items)})


def producto(id_, precio="10.50", nombre="Cámara IP", **campos):
    return {"id": id_, "nombre": nombre, "precio": precio, **campos}


class ApiFalsa:
    """Transporte httpx simulado: responde por (host, ruta) y guarda cada petición.

    Cada ruta tiene una lista de respuestas que se consumen en orden; la última se repite.
    Una respuesta puede ser una clase de excepción de httpx, que se lanza con la petición.
    """

    def __init__(self):
        self.rutas = {}
        self.peticiones = []

    def responder(self, host, ruta, *respuestas):
        self.rutas[(host, ruta)] = list(respuestas)

    def __call__(self, request):
        self.peticiones.append(request)
        pendientes = self.rutas[(request.url.host, request.url.path)]
        respuesta = pendientes.pop(0) if len(pendientes) > 1 else pendientes[0]
        if isinstance(respuesta, type) and issubclass(respuesta, Exception):
            raise respuesta("simulado", request=request)
        return respuesta

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def rutas_pedidas(self):
        return [f"{p.url.host}{p.url.path}" for p in self.peticiones]


# --- M3: las pruebas nunca tocan la API real ni las credenciales reales (docs/16 §6.2) ---


@contextmanager
def sin_red():
    """Corta cualquier petición por un transporte real de httpx y, al salir, hace fallar la prueba.

    ApiFalsa usa httpx.MockTransport, que no pasa por aquí. El fallo se comprueba al salir
    porque el núcleo convierte las excepciones en errores de resultado.
    """
    intentos = []

    def bloquear(transporte, request):
        intentos.append(f"{request.method} {request.url.host}")
        raise httpx.ConnectError("Red bloqueada en las pruebas.", request=request)

    async def bloquear_async(transporte, request):
        return bloquear(transporte, request)

    with (
        mock.patch.object(httpx.HTTPTransport, "handle_request", bloquear),
        mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", bloquear_async),
    ):
        yield intentos
    if intentos:
        raise AssertionError(f"Una prueba intentó salir a la red: {intentos}")


@contextmanager
def credenciales_de_prueba(code, **valores):
    """Oculta las variables reales del proveedor (cualquier nombre que contenga su code) y pone
    valores falsos como PROVIDER_<CODE>_<CLAVE>. Al salir se restaura el entorno."""
    marca = code.upper().replace("-", "_")
    with mock.patch.dict(os.environ):
        for nombre in [n for n in os.environ if marca in n.upper()]:
            del os.environ[nombre]
        os.environ.update({f"PROVIDER_{marca}_{clave}": valor for clave, valor in valores.items()})
        yield
