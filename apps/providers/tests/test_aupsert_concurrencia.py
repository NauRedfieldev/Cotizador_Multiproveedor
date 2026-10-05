"""Bloque C: concurrencia de aupsert.

Limitación: con asyncio.gather el ORM async de Django ejecuta todas las consultas en un solo
hilo (sync_to_async con thread_sensitive=True), así que esas variantes detectan el intercalado
entre awaits, no paralelismo real en la BD. Las variantes "con hilos reales" usan una conexión
por hilo y sí ponen a competir transacciones en PostgreSQL.
"""
import asyncio
from collections import Counter

from asgiref.sync import async_to_sync
from django.test import TransactionTestCase, tag

from apps.providers.models import RawProviderProduct
from apps.providers.tests.soporte import (
    PAYLOAD,
    crear_proveedor,
    ejecutar_en_hilos,
    hash_independiente,
)

PAYLOADS_DISTINTOS = [{"precio": precio, "sku": "X1"} for precio in range(100, 110)]


def excepciones(resultados):
    return [r for r in resultados if isinstance(r, BaseException)]


class BaseConcurrencia(TransactionTestCase):
    def setUp(self):
        self.proveedor = crear_proveedor()

    async def upserts_simultaneos(self, payloads, external_id="X1"):
        return await asyncio.gather(
            *(
                RawProviderProduct.objects.aupsert(self.proveedor, external_id, payload)
                for payload in payloads
            ),
            return_exceptions=True,
        )

    def upserts_en_hilos(self, payloads, external_id="X1"):
        return ejecutar_en_hilos(
            RawProviderProduct.objects.aupsert,
            [(self.proveedor, external_id, payload) for payload in payloads],
        )


@tag("django_db", "concurrencia")
class PruebasAltasSimultaneas(BaseConcurrencia):
    async def test_veinte_altas_simultaneas_no_lanzan_excepciones(self):
        """C1 ⭐ (gather) 20 altas simultáneas de una clave nueva no lanzan, tampoco IntegrityError."""
        # Act
        resultados = await self.upserts_simultaneos([PAYLOAD] * 20)

        # Assert
        self.assertEqual(excepciones(resultados), [])

    async def test_veinte_altas_simultaneas_dejan_una_fila_con_fetch_count_20(self):
        """C1 ⭐ (gather) 20 altas simultáneas de una clave nueva dejan una fila con fetch_count=20."""
        # Act
        await self.upserts_simultaneos([PAYLOAD] * 20)

        # Assert
        contadores = [fila.fetch_count async for fila in RawProviderProduct.objects.all()]
        self.assertEqual(contadores, [20])

    def test_veinte_altas_en_hilos_reales_no_lanzan_excepciones(self):
        """C1 ⭐ (hilos reales) 20 altas simultáneas de una clave nueva no lanzan, tampoco IntegrityError."""
        # Act
        resultados = self.upserts_en_hilos([PAYLOAD] * 20)

        # Assert
        self.assertEqual(excepciones(resultados), [])

    def test_veinte_altas_en_hilos_reales_dejan_una_fila_con_fetch_count_20(self):
        """C1 ⭐ (hilos reales) 20 altas simultáneas de una clave nueva dejan una fila con fetch_count=20."""
        # Act
        self.upserts_en_hilos([PAYLOAD] * 20)

        # Assert
        contadores = list(RawProviderProduct.objects.values_list("fetch_count", flat=True))
        self.assertEqual(contadores, [20])

    def test_altas_simultaneas_del_mismo_payload_devuelven_un_created_y_el_resto_unchanged(self):
        """C1 20 altas simultáneas del mismo payload: 1 created y 19 unchanged (HALLAZGO-4, corregido)."""
        # Act
        resultados = async_to_sync(self.upserts_simultaneos)([PAYLOAD] * 20)

        # Assert
        estados = Counter(estado for _, estado in resultados)
        self.assertEqual(estados, Counter({"created": 1, "unchanged": 19}))


@tag("django_db", "concurrencia")
class PruebasIncrementosSimultaneos(BaseConcurrencia):
    def setUp(self):
        super().setUp()
        async_to_sync(RawProviderProduct.objects.aupsert)(self.proveedor, "X1", PAYLOAD)

    async def test_cincuenta_llamadas_simultaneas_no_pierden_incrementos(self):
        """C2 ⭐ (gather) 50 aupserts simultáneos del mismo payload sobre una fila existente dejan fetch_count=51."""
        # Act
        await self.upserts_simultaneos([PAYLOAD] * 50)

        # Assert
        fila = await RawProviderProduct.objects.aget()
        self.assertEqual(fila.fetch_count, 51)

    def test_cincuenta_hilos_reales_no_pierden_incrementos(self):
        """C2 ⭐ (hilos reales) 50 aupserts simultáneos del mismo payload sobre una fila existente dejan fetch_count=51."""
        # Act
        self.upserts_en_hilos([PAYLOAD] * 50)

        # Assert
        self.assertEqual(RawProviderProduct.objects.get().fetch_count, 51)


@tag("django_db", "concurrencia")
class PruebasPayloadsDistintosSimultaneos(BaseConcurrencia):
    async def test_payloads_distintos_simultaneos_dejan_una_sola_fila(self):
        """C3 ⭐ (gather) Payloads distintos y simultáneos sobre la misma clave dejan una sola fila."""
        # Act
        await self.upserts_simultaneos(PAYLOADS_DISTINTOS)

        # Assert
        self.assertEqual(await RawProviderProduct.objects.acount(), 1)

    async def test_payloads_distintos_simultaneos_dejan_un_hash_coherente_con_el_payload_guardado(self):
        """C3 ⭐ (gather) El content_hash final corresponde al payload que quedó guardado."""
        # Act
        await self.upserts_simultaneos(PAYLOADS_DISTINTOS)

        # Assert
        fila = await RawProviderProduct.objects.aget()
        self.assertEqual(fila.content_hash, hash_independiente(fila.payload))

    def test_payloads_distintos_en_hilos_reales_dejan_un_hash_coherente_con_el_payload_guardado(self):
        """C3 ⭐ (hilos reales) El content_hash final corresponde al payload que quedó guardado."""
        # Act
        self.upserts_en_hilos(PAYLOADS_DISTINTOS)

        # Assert
        fila = RawProviderProduct.objects.get()
        self.assertEqual(fila.content_hash, hash_independiente(fila.payload))


@tag("django_db", "concurrencia")
class PruebasClavesDistintasSimultaneas(BaseConcurrencia):
    async def test_claves_distintas_simultaneas_crean_una_fila_por_clave(self):
        """C4 15 aupserts simultáneos con claves distintas crean 15 filas."""
        # Act
        await asyncio.gather(
            *(
                RawProviderProduct.objects.aupsert(self.proveedor, f"X{i}", PAYLOAD)
                for i in range(15)
            ),
            return_exceptions=True,
        )

        # Assert
        self.assertEqual(await RawProviderProduct.objects.acount(), 15)
