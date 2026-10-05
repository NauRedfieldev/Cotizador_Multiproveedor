"""SDD M2 §9, pruebas 17–20: registro de adaptadores (sin BD)."""
from unittest import mock

from django.apps import apps as django_apps
from django.test import SimpleTestCase, tag

from apps.providers import registry
from apps.providers.adapters.base import ProviderAdapter


def adaptador(code, nombre="Adaptador"):
    """Crea una clase de adaptador mínima con el code indicado."""

    async def fetch(self, query, client):
        return []

    def parse_offer(self, item):
        raise NotImplementedError

    atributos = {"fetch": fetch, "parse_offer": parse_offer}
    if code is not None:
        atributos["code"] = code
    return type(nombre, (ProviderAdapter,), atributos)


@tag("unit")
class PruebasRegistro(SimpleTestCase):
    def setUp(self):
        # Cada prueba trabaja con un registro vacío; el real se restaura al terminar.
        self.enterContext(mock.patch.dict(registry._REGISTRY, {}, clear=True))

    def test_un_adaptador_registrado_se_recupera_por_su_code(self):
        """17 @register guarda el adaptador y get_adapter_class lo devuelve por su code."""
        # Arrange
        demo = adaptador("demo")

        # Act
        registry.register(demo)

        # Assert
        self.assertIs(registry.get_adapter_class("demo"), demo)

    def test_registered_codes_sale_ordenado(self):
        """17 registered_codes devuelve los códigos ordenados."""
        # Arrange
        registry.register(adaptador("zeta"))
        registry.register(adaptador("alfa"))

        # Act
        codigos = registry.registered_codes()

        # Assert
        self.assertEqual(codigos, ["alfa", "zeta"])

    def test_un_code_repetido_lanza_value_error(self):
        """18 Registrar dos adaptadores con el mismo code se rechaza."""
        # Arrange
        registry.register(adaptador("demo"))

        # Act / Assert
        with self.assertRaisesMessage(ValueError, "Adaptador duplicado para el proveedor 'demo'."):
            registry.register(adaptador("demo"))

    def test_un_adaptador_sin_code_lanza_value_error(self):
        """19 Un adaptador sin code se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "El adaptador SinCode no define code."):
            registry.register(adaptador(None, nombre="SinCode"))

    def test_un_code_que_no_es_slug_lanza_value_error(self):
        """19 Un code con mayúsculas o espacios se rechaza."""
        # Act / Assert
        with self.assertRaisesMessage(ValueError, "Código de adaptador inválido: 'Demo X'."):
            registry.register(adaptador("Demo X"))

    def test_un_code_desconocido_devuelve_none(self):
        """20 get_adapter_class de un code sin adaptador devuelve None."""
        # Act / Assert
        self.assertIsNone(registry.get_adapter_class("inexistente"))


@tag("unit")
class PruebasArranque(SimpleTestCase):
    def test_ready_importa_el_paquete_de_adaptadores(self):
        """17 ProvidersConfig.ready() importa apps.providers.adapters para que se registren al arrancar."""
        # Arrange
        config = django_apps.get_app_config("providers")

        # Act
        with mock.patch("apps.providers.apps.import_module") as importar:
            config.ready()

        # Assert
        importar.assert_called_once_with("apps.providers.adapters")
