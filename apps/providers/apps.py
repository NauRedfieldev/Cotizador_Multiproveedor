from importlib import import_module

from django.apps import AppConfig


class ProvidersConfig(AppConfig):
    name = 'apps.providers'

    def ready(self):
        # Importa los adaptadores para que su @register se ejecute al arrancar (SDD M2 §6.1).
        import_module(f"{self.name}.adapters")
