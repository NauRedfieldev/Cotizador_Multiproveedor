"""Bloque B: comportamiento de RawProviderProduct.objects.aupsert."""
import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from asgiref.sync import async_to_sync
from django.db import IntegrityError, connection
from django.test import TestCase, TransactionTestCase, tag
from django.test.utils import CaptureQueriesContext

from apps.providers.models import Provider, RawProviderProduct, RawProviderProductManager
from apps.providers.tests.soporte import (
    HASH_PAYLOAD,
    HASH_PAYLOAD_NUEVO,
    PAYLOAD,
    PAYLOAD_NUEVO,
    T0,
    T1,
    RelojControlado,
    crear_proveedor,
    hash_independiente,
)


def campos(obj):
    return (
        obj.external_id,
        obj.payload,
        obj.content_hash,
        obj.first_seen_at,
        obj.last_seen_at,
        obj.fetch_count,
    )


class BaseAupsert(TestCase):
    """Proveedor de prueba y reloj controlado que arranca en T0."""

    @classmethod
    def setUpTestData(cls):
        cls.proveedor = crear_proveedor()

    def setUp(self):
        self.reloj = self.enterContext(RelojControlado(T0))

    async def upsert(self, payload, external_id="X1", proveedor=None):
        return await RawProviderProduct.objects.aupsert(
            proveedor or self.proveedor, external_id, payload
        )


@tag("django_db")
class PruebasAupsertAlta(BaseAupsert):
    async def test_producto_nuevo_fija_first_y_last_seen_at_al_instante_actual(self):
        """B1 ⭐ Al crear, first_seen_at y last_seen_at valen el instante actual."""
        # Act
        obj, _ = await self.upsert(PAYLOAD)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual((guardado.first_seen_at, guardado.last_seen_at), (T0, T0))

    async def test_producto_nuevo_guarda_el_hash_de_la_formula_documentada(self):
        """B1 ⭐ Al crear, content_hash es el sha256 canónico del payload."""
        # Act
        obj, _ = await self.upsert(PAYLOAD)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(guardado.content_hash, HASH_PAYLOAD)


@tag("django_db")
class PruebasAupsertSinCambios(BaseAupsert):
    async def test_mismo_payload_mueve_last_seen_at_al_instante_actual(self):
        """B2 ⭐ La rama unchanged actualiza last_seen_at."""
        # Arrange
        await self.upsert(PAYLOAD)
        self.reloj.ahora = T1

        # Act
        obj, _ = await self.upsert(PAYLOAD)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(guardado.last_seen_at, T1)

    async def test_mismo_payload_no_modifica_payload_hash_ni_first_seen_at(self):
        """B2 ⭐ La rama unchanged no reescribe el contenido ni first_seen_at."""
        # Arrange
        await self.upsert(PAYLOAD)
        self.reloj.ahora = T1

        # Act
        obj, _ = await self.upsert(PAYLOAD)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(
            (guardado.payload, guardado.content_hash, guardado.first_seen_at),
            (PAYLOAD, HASH_PAYLOAD, T0),
        )


@tag("django_db")
class PruebasConsultasPorRama(TestCase):
    """B6: se mide desde un test síncrono para que las consultas pasen por esta conexión."""

    @classmethod
    def setUpTestData(cls):
        cls.proveedor = crear_proveedor()

    def setUp(self):
        async_to_sync(RawProviderProduct.objects.aupsert)(self.proveedor, "X1", PAYLOAD)

    def sentencias_de(self, external_id, payload):
        with CaptureQueriesContext(connection) as consultas:
            async_to_sync(RawProviderProduct.objects.aupsert)(self.proveedor, external_id, payload)
        return [c["sql"].split()[0].upper() for c in consultas.captured_queries]

    def test_rama_unchanged_ejecuta_un_solo_update(self):
        """B6 ⭐ La rama unchanged ejecuta un solo UPDATE (HALLAZGO-3, corregido)."""
        # Act
        sentencias = self.sentencias_de("X1", PAYLOAD)

        # Assert
        self.assertEqual(sentencias, ["UPDATE"])

    def test_rama_created_ejecuta_update_e_insert(self):
        """B6 La rama created ejecuta el UPDATE que no encuentra fila y el INSERT."""
        # Act
        sentencias = self.sentencias_de("X2", PAYLOAD)

        # Assert
        self.assertEqual(sentencias, ["UPDATE", "INSERT"])

    def test_rama_updated_ejecuta_update_e_insert_on_conflict(self):
        """B6 La rama updated ejecuta el UPDATE que no encuentra el hash y el INSERT … ON CONFLICT."""
        # Act
        sentencias = self.sentencias_de("X1", PAYLOAD_NUEVO)

        # Assert
        self.assertEqual(sentencias, ["UPDATE", "INSERT"])


@tag("django_db")
class PruebasAupsertConCambios(BaseAupsert):
    async def test_payload_distinto_mueve_last_seen_at_al_instante_actual(self):
        """B3 ⭐ La rama updated actualiza last_seen_at."""
        # Arrange
        await self.upsert(PAYLOAD)
        self.reloj.ahora = T1

        # Act
        obj, _ = await self.upsert(PAYLOAD_NUEVO)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(guardado.last_seen_at, T1)

    async def test_payload_distinto_guarda_el_hash_de_la_formula_documentada(self):
        """B3 ⭐ La rama updated guarda el hash canónico del payload nuevo."""
        # Arrange
        await self.upsert(PAYLOAD)

        # Act
        obj, _ = await self.upsert(PAYLOAD_NUEVO)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(guardado.content_hash, HASH_PAYLOAD_NUEVO)

    async def test_secuencia_a_b_a_devuelve_updated_las_dos_veces(self):
        """B12 A→B→A: volver al contenido inicial también es updated."""
        # Act
        estados = [
            (await self.upsert(PAYLOAD))[1],
            (await self.upsert(PAYLOAD_NUEVO))[1],
            (await self.upsert(PAYLOAD))[1],
        ]

        # Assert
        self.assertEqual(estados, ["created", "updated", "updated"])

    async def test_secuencia_a_b_a_deja_el_hash_de_a(self):
        """B12 A→B→A: el hash final vuelve a ser el de A."""
        # Arrange
        await self.upsert(PAYLOAD)
        await self.upsert(PAYLOAD_NUEVO)

        # Act
        obj, _ = await self.upsert(PAYLOAD)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(guardado.content_hash, HASH_PAYLOAD)


@tag("django_db")
class PruebasAupsertRepetido(BaseAupsert):
    async def test_cien_llamadas_con_el_mismo_contenido_en_distinto_orden_crean_una_sola_vez(self):
        """B4 ⭐ 100 llamadas con el mismo contenido alternando el orden de las claves: 1 created y 99 unchanged."""
        # Arrange
        variantes = ({"precio": 100, "sku": "X1"}, {"sku": "X1", "precio": 100})

        # Act
        estados = []
        for i in range(100):
            _, estado = await self.upsert(variantes[i % 2])
            estados.append(estado)

        # Assert
        self.assertEqual(estados, ["created"] + ["unchanged"] * 99)


@tag("django_db")
class PruebasObjetoDevuelto(BaseAupsert):
    async def test_objeto_devuelto_al_crear_coincide_con_la_bd(self):
        """B5 ⭐ Rama created: el objeto devuelto refleja la fila guardada."""
        # Act
        obj, _ = await self.upsert(PAYLOAD)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(campos(obj), campos(guardado))

    async def test_objeto_devuelto_sin_cambios_coincide_con_la_bd(self):
        """B5 ⭐ Rama unchanged: el objeto devuelto refleja la fila guardada."""
        # Arrange
        await self.upsert(PAYLOAD)
        self.reloj.ahora = T1

        # Act
        obj, _ = await self.upsert(PAYLOAD)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(campos(obj), campos(guardado))

    async def test_objeto_devuelto_al_actualizar_coincide_con_la_bd(self):
        """B5 ⭐ Rama updated: el objeto devuelto refleja la fila guardada."""
        # Arrange
        await self.upsert(PAYLOAD)
        self.reloj.ahora = T1

        # Act
        obj, _ = await self.upsert(PAYLOAD_NUEVO)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(campos(obj), campos(guardado))

    async def test_aupsert_devuelve_obj_y_estado_segun_la_rama(self):
        """B7 Devuelve una tupla (obj, estado) con created, unchanged y updated según la rama."""
        # Act
        respuestas = [
            await self.upsert(PAYLOAD),
            await self.upsert(PAYLOAD),
            await self.upsert(PAYLOAD_NUEVO),
        ]

        # Assert
        forma = [(type(r).__name__, len(r), type(r[0]).__name__, r[1]) for r in respuestas]
        self.assertEqual(
            forma,
            [
                ("tuple", 2, "RawProviderProduct", "created"),
                ("tuple", 2, "RawProviderProduct", "unchanged"),
                ("tuple", 2, "RawProviderProduct", "updated"),
            ],
        )


@tag("django_db")
class PruebasAupsertVariosProveedores(BaseAupsert):
    async def test_actualizar_un_proveedor_no_toca_la_fila_del_otro(self):
        """B8 Mismo external_id en dos proveedores: actualizar uno no modifica la fila del otro."""
        # Arrange
        otro = await Provider.objects.acreate(
            code="otro", name="Otro", base_url="https://otro.example.com"
        )
        await self.upsert(PAYLOAD)
        await self.upsert(PAYLOAD, proveedor=otro)
        self.reloj.ahora = T1

        # Act
        await self.upsert(PAYLOAD_NUEVO)

        # Assert
        fila_otro = await RawProviderProduct.objects.aget(provider=otro, external_id="X1")
        self.assertEqual(
            (fila_otro.payload, fila_otro.content_hash, fila_otro.last_seen_at, fila_otro.fetch_count),
            (PAYLOAD, HASH_PAYLOAD, T0, 1),
        )


@tag("django_db")
class PruebasAupsertContratoDeDatos(BaseAupsert):
    async def test_external_id_entero_y_texto_equivalente_usan_la_misma_fila(self):
        """B9 Contrato (decisión 3): external_id se normaliza con str(), así que 123 y "123" son la misma fila."""
        # Arrange
        await self.upsert(PAYLOAD, external_id=123)

        # Act
        _, estado = await self.upsert(PAYLOAD, external_id="123")

        # Assert
        filas = await RawProviderProduct.objects.acount()
        self.assertEqual((estado, filas), ("unchanged", 1))

    async def test_external_id_entero_devuelve_el_objeto_con_texto(self):
        """B9 Contrato (decisión 3): el objeto devuelto lleva el external_id normalizado a texto."""
        # Act
        obj, _ = await self.upsert(PAYLOAD, external_id=123)

        # Assert
        self.assertEqual(obj.external_id, "123")

    async def test_external_id_vacio_lanza_value_error(self):
        """B14 Contrato (decisión 3): un external_id vacío se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "external_id no puede estar vacío."):
            await self.upsert(PAYLOAD, external_id="")

    async def test_external_id_solo_con_espacios_lanza_value_error(self):
        """B14 Contrato (decisión 3): un external_id con solo espacios se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "external_id no puede estar vacío."):
            await self.upsert(PAYLOAD, external_id="   ")

    async def test_proveedor_no_guardado_lanza_value_error(self):
        """B10 Un Provider sin guardar se rechaza antes de llegar a la BD."""
        # Arrange
        sin_guardar = Provider(code="fantasma", name="Fantasma", base_url="https://fantasma.example.com")

        # Act / Assert
        with self.assertRaisesMessage(
            ValueError, "El proveedor debe estar guardado antes de llamar a aupsert."
        ):
            await self.upsert(PAYLOAD, proveedor=sin_guardar)

    async def test_payload_json_puro_vuelve_igual_desde_la_bd(self):
        """B13 Un payload con tipos JSON puros vuelve de la BD tal como se envió."""
        # Arrange
        payload = {
            "sku": "X1",
            "precio": 100,
            "activo": True,
            "descuento": None,
            "tallas": ["S", "M"],
            "dimensiones": {"alto": 10, "unidad": "cm"},
        }

        # Act
        obj, _ = await self.upsert(payload)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(guardado.payload, payload)

    async def test_decimal_se_guarda_y_se_lee_como_texto(self):
        """B13 Contrato (decisión 1): un Decimal se guarda y se lee como texto."""
        # Act
        obj, _ = await self.upsert({"precio": Decimal("19.99")})

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(guardado.payload, {"precio": "19.99"})

    async def test_content_hash_corresponde_al_payload_tal_como_se_guarda(self):
        """B13 Contrato (decisión 1): content_hash es el sha256 del JSON que queda en la BD."""
        # Arrange
        payload = {
            "precio": Decimal("19.90"),
            "visto": datetime(2026, 10, 4, 12, 30, 0, 123456, tzinfo=dt_timezone.utc),
            "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        }

        # Act
        obj, _ = await self.upsert(payload)

        # Assert
        guardado = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(hash_independiente(guardado.payload), guardado.content_hash)

    async def test_tras_tres_intentos_sin_fila_lanza_runtime_error(self):
        """B15 Si la fila cambia en cada intento, aupsert se rinde con RuntimeError en vez de reintentar sin fin."""
        # Arrange
        sin_fila = mock.AsyncMock(return_value=None)

        # Act / Assert
        with mock.patch.object(RawProviderProductManager, "_afila", sin_fila):
            with self.assertRaisesMessage(RuntimeError, "tras 3 intentos"):
                await self.upsert(PAYLOAD)


@tag("django_db")
class PruebasAupsertProveedorBorrado(TransactionTestCase):
    """B10 necesita transacciones reales: la FK es diferida y solo falla al confirmar."""

    async def test_caracterizacion_proveedor_borrado_lanza_integrity_error_al_confirmar(self):
        """B10 CARACTERIZACIÓN: si el Provider se borró mientras tanto, la FK diferida falla al confirmar."""
        # Arrange
        proveedor = await Provider.objects.acreate(
            code="efimero", name="Efímero", base_url="https://efimero.example.com"
        )
        await Provider.objects.filter(pk=proveedor.pk).adelete()

        # Act / Assert
        with self.assertRaises(IntegrityError):
            await RawProviderProduct.objects.aupsert(proveedor, "X1", PAYLOAD)
