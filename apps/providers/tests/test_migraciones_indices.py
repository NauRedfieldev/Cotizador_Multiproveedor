"""Bloque E: migraciones e índices, comprobados contra el catálogo de PostgreSQL."""
from io import StringIO

from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, tag

from apps.providers.models import RawProviderProduct
from apps.providers.tests.soporte import T0, crear_proveedor, exigir_bd_de_pruebas, hash_independiente

TABLAS_PROVIDERS = {"providers_provider", "raw_provider_product", "providers_providertoken"}

# Índices de una tabla filtrados por método de acceso (gin, btree…) y por su primera columna.
SQL_INDICES_POR_METODO_Y_COLUMNA = """
    SELECT i.relname
    FROM pg_index x
    JOIN pg_class i ON i.oid = x.indexrelid
    JOIN pg_class t ON t.oid = x.indrelid
    JOIN pg_am am ON am.oid = i.relam
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = x.indkey[0]
    WHERE t.relname = %s AND am.amname = %s AND a.attname = %s
"""

SQL_COLUMNAS_DE_RESTRICCIONES_UNICAS = """
    SELECT c.conname, array_agg(a.attname::text ORDER BY a.attname::text)
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
    WHERE t.relname = %s AND c.contype = 'u'
    GROUP BY c.conname
"""

SQL_TIPO_DE_COLUMNA = """
    SELECT data_type, character_maximum_length
    FROM information_schema.columns
    WHERE table_name = %s AND column_name = %s
"""


def consultar(sql, parametros):
    with connection.cursor() as cursor:
        cursor.execute(sql, parametros)
        return cursor.fetchall()


@tag("django_db")
class PruebasMigraciones(TestCase):
    def test_no_hay_cambios_de_modelo_sin_migracion(self):
        """E1 makemigrations --check --dry-run no detecta cambios pendientes."""
        # Arrange
        salida = StringIO()
        codigo = 0

        # Act
        try:
            call_command("makemigrations", "--check", "--dry-run", stdout=salida, stderr=salida)
        except SystemExit as fin:
            codigo = fin.code

        # Assert
        self.assertEqual(codigo, 0, salida.getvalue())


@tag("django_db")
class PruebasMigracionReversible(TransactionTestCase):
    def test_la_migracion_de_providers_se_deshace_y_se_vuelve_a_aplicar(self):
        """E2 migrate providers zero elimina sus tablas y migrate las vuelve a crear (solo en la BD de pruebas)."""
        # Arrange
        exigir_bd_de_pruebas()

        # Act
        try:
            call_command("migrate", "providers", "zero", verbosity=0)
            tablas_tras_deshacer = set(connection.introspection.table_names())
        finally:
            call_command("migrate", "providers", verbosity=0)
        tablas_tras_rehacer = set(connection.introspection.table_names())

        # Assert
        self.assertEqual(
            (TABLAS_PROVIDERS & tablas_tras_deshacer, TABLAS_PROVIDERS & tablas_tras_rehacer),
            (set(), TABLAS_PROVIDERS),
        )


@tag("django_db")
class PruebasEsquemaRawProviderProduct(TestCase):
    def test_payload_es_jsonb(self):
        """E3 La columna payload es jsonb."""
        # Act
        tipo = consultar(SQL_TIPO_DE_COLUMNA, ["raw_provider_product", "payload"])

        # Assert
        self.assertEqual(tipo, [("jsonb", None)])

    def test_existe_un_indice_gin_sobre_payload(self):
        """E4 ⭐ Hay un índice GIN sobre payload (se busca por método y columna, no por nombre)."""
        # Act
        indices = consultar(SQL_INDICES_POR_METODO_Y_COLUMNA, ["raw_provider_product", "gin", "payload"])

        # Assert
        self.assertNotEqual(indices, [])

    def test_existe_un_indice_btree_sobre_last_seen_at(self):
        """E5 Hay un índice btree cuya primera columna es last_seen_at."""
        # Act
        indices = consultar(
            SQL_INDICES_POR_METODO_Y_COLUMNA, ["raw_provider_product", "btree", "last_seen_at"]
        )

        # Assert
        self.assertNotEqual(indices, [])

    def test_existe_la_restriccion_unica_sobre_provider_y_external_id(self):
        """E6 Hay una restricción UNIQUE sobre (provider_id, external_id) en pg_constraint."""
        # Act
        restricciones = consultar(SQL_COLUMNAS_DE_RESTRICCIONES_UNICAS, ["raw_provider_product"])

        # Assert
        self.assertIn(["external_id", "provider_id"], [columnas for _, columnas in restricciones])

    def test_content_hash_es_varchar_64(self):
        """E7 content_hash es character varying(64)."""
        # Act
        tipo = consultar(SQL_TIPO_DE_COLUMNA, ["raw_provider_product", "content_hash"])

        # Assert
        self.assertEqual(tipo, [("character varying", 64)])

    def test_no_quedan_tablas_ni_modelos_del_tutorial(self):
        """E8 Sin restos del tutorial: ni tablas ni modelos Question/Choice."""
        # Act
        tablas = {"providers_question", "providers_choice"} & set(connection.introspection.table_names())
        modelos = {"Question", "Choice"} & {modelo.__name__ for modelo in apps.get_models()}

        # Assert
        self.assertEqual((tablas, modelos), (set(), set()))

    def test_consulta_de_contencion_jsonb_devuelve_la_fila_correcta(self):
        """E9 Filtrar por payload @> {"a": 1} devuelve solo la fila que lo contiene."""
        # Arrange
        proveedor = crear_proveedor()
        RawProviderProduct.objects.bulk_create(
            [
                RawProviderProduct(
                    provider=proveedor,
                    external_id=external_id,
                    content_hash=hash_independiente(payload),
                    payload=payload,
                    first_seen_at=T0,
                    last_seen_at=T0,
                )
                for external_id, payload in (("X1", {"a": 1, "b": 2}), ("X2", {"a": 2}), ("X3", {"b": 1}))
            ]
        )

        # Act
        encontrados = list(
            RawProviderProduct.objects.filter(payload__contains={"a": 1}).values_list("external_id", flat=True)
        )

        # Assert
        self.assertEqual(encontrados, ["X1"])
