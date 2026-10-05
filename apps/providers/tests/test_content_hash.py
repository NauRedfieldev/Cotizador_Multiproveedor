"""Bloque A: compute_content_hash (sin BD)."""
import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import SimpleTestCase, tag

from apps.providers.models import compute_content_hash
from apps.providers.tests.soporte import hash_independiente

# sha256('{}'), calculado a mano.
HASH_VACIO = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"


@tag("unit")
class PruebasHashDeContenido(SimpleTestCase):
    def test_vector_dorado_coincide_con_la_formula_documentada(self):
        """A1 ⭐ El hash de {"b": 2, "a": 1} sigue la fórmula documentada."""
        # Arrange
        payload = {"b": 2, "a": 1}
        esperado = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        # Act
        resultado = compute_content_hash(payload)

        # Assert
        self.assertEqual(resultado, esperado)

    def test_un_valor_distinto_produce_un_hash_distinto(self):
        """A3 Cambiar un valor cambia el hash."""
        # Arrange
        original = {"precio": 100, "sku": "X1"}
        modificado = {"precio": 101, "sku": "X1"}

        # Act
        hashes = (compute_content_hash(original), compute_content_hash(modificado))

        # Assert
        self.assertNotEqual(*hashes)

    def test_el_hash_son_64_caracteres_hexadecimales_en_minuscula(self):
        """A4 El hash es un SHA-256 en hexadecimal en minúscula."""
        # Act
        resultado = compute_content_hash({"sku": "X1"})

        # Assert
        self.assertRegex(resultado, r"\A[0-9a-f]{64}\Z")

    def test_el_hash_es_determinista_entre_llamadas(self):
        """A5 Dos llamadas con el mismo contenido dan el mismo hash."""
        # Arrange
        payload = {"sku": "X1", "atributos": {"color": "rojo", "tallas": ["S", "M"]}}
        copia = copy.deepcopy(payload)

        # Act
        primero = compute_content_hash(payload)
        segundo = compute_content_hash(copia)

        # Assert
        self.assertEqual(primero, segundo)

    def test_reordenar_una_lista_cambia_el_hash(self):
        """A6 Contrato (decisión 1): sort_keys solo ordena claves; el orden de las listas es contenido."""
        # Arrange
        original = {"tallas": ["S", "M"]}
        reordenado = {"tallas": ["M", "S"]}

        # Act
        hashes = (compute_content_hash(original), compute_content_hash(reordenado))

        # Assert
        self.assertNotEqual(*hashes)

    def test_texto_con_acentos_enie_y_emoji_sigue_la_formula_documentada(self):
        """A7 Acentos, ñ y emoji no lanzan y siguen la fórmula documentada."""
        # Arrange
        payload = {"nombre": "Cañón eléctrico ⚡", "marca": "Ñandú"}

        # Act
        resultado = compute_content_hash(payload)

        # Assert
        self.assertEqual(resultado, hash_independiente(payload))

    def test_decimal_1_10_y_1_1_dan_hashes_distintos(self):
        """A8 Contrato (decisión 1): un Decimal se guarda con str() y conserva los ceros, y el hash también."""
        # Act
        hashes = (
            compute_content_hash({"precio": Decimal("1.10")}),
            compute_content_hash({"precio": Decimal("1.1")}),
        )

        # Assert
        self.assertNotEqual(*hashes)

    def test_decimal_y_float_equivalentes_dan_hashes_distintos(self):
        """A8 Contrato (decisión 1): Decimal se guarda como texto ("1.1") y float como número (1.1)."""
        # Act
        hashes = (
            compute_content_hash({"precio": Decimal("1.1")}),
            compute_content_hash({"precio": 1.1}),
        )

        # Assert
        self.assertNotEqual(*hashes)

    def test_decimal_y_texto_equivalente_dan_el_mismo_hash(self):
        """A8 Contrato (decisión 1): Decimal("1.1") y "1.1" se guardan igual, así que comparten hash a propósito."""
        # Act
        hashes = (
            compute_content_hash({"precio": Decimal("1.1")}),
            compute_content_hash({"precio": "1.1"}),
        )

        # Assert
        self.assertEqual(*hashes)

    def test_payload_con_datetime_se_serializa_en_iso_8601(self):
        """A9 Un datetime no lanza: se serializa en ISO 8601 (DjangoJSONEncoder)."""
        # Arrange
        payload = {"visto": datetime(2026, 10, 4, 12, 30, tzinfo=dt_timezone.utc)}

        # Act
        resultado = compute_content_hash(payload)

        # Assert
        self.assertEqual(resultado, hash_independiente({"visto": "2026-10-04T12:30:00Z"}))

    def test_payload_con_uuid_se_serializa_como_texto(self):
        """A9 Un UUID no lanza: se serializa como texto (DjangoJSONEncoder)."""
        # Arrange
        payload = {"id": uuid.UUID("12345678-1234-5678-1234-567812345678")}

        # Act
        resultado = compute_content_hash(payload)

        # Assert
        self.assertEqual(
            resultado, hash_independiente({"id": "12345678-1234-5678-1234-567812345678"})
        )

    def test_datetimes_que_solo_difieren_en_microsegundos_dan_el_mismo_hash(self):
        """A9 Contrato (decisión 1): los datetime se guardan truncados a milisegundos y el hash sigue a lo guardado."""
        # Arrange
        base = datetime(2026, 10, 4, 12, 30, 0, 123456, tzinfo=dt_timezone.utc)
        casi_igual = base.replace(microsecond=123999)

        # Act
        hashes = (compute_content_hash({"visto": base}), compute_content_hash({"visto": casi_igual}))

        # Assert
        self.assertEqual(*hashes)

    def test_un_set_lanza_type_error(self):
        """A10 Contrato (decisión 1): un tipo que JSON no admite lanza TypeError (tampoco podría guardarse)."""
        # Act / Assert
        with self.assertRaisesMessage(TypeError, "Object of type set is not JSON serializable"):
            compute_content_hash({"tallas": {"S", "M"}})

    def test_payload_vacio_tiene_un_hash_valido_y_estable(self):
        """A11 El payload vacío tiene el hash de '{}'."""
        # Act
        resultado = compute_content_hash({})

        # Assert
        self.assertEqual(resultado, HASH_VACIO)

    def test_no_muta_el_payload_de_entrada(self):
        """A12 Calcular el hash no cambia ni el contenido ni el orden del payload."""
        # Arrange
        payload = {"sku": "X1", "atributos": {"tallas": ["M", "S"], "color": "rojo"}}
        antes = json.dumps(payload)

        # Act
        compute_content_hash(payload)

        # Assert
        self.assertEqual(json.dumps(payload), antes)
