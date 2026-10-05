"""Utilidades compartidas por las pruebas de apps.providers.

El runner de Django no carga conftest.py, así que lo común vive aquí.
"""
import hashlib
import json
import threading
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from asgiref.sync import async_to_sync
from django.db import connection, connections

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
