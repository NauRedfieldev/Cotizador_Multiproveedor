from importlib import import_module

from django.apps import AppConfig


class ProvidersWebConfig(AppConfig):
    name = 'apps.providers_web'

    def ready(self):
        # Importa los scrapers para que su @register (el registro de apps.providers) se ejecute
        # al arrancar; search_all los consulta igual que a los adaptadores de API (SDD M9).
        import_module(f"{self.name}.scrapers")
