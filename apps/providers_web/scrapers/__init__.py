"""Scrapers de proveedores sin API (SDD M9, docs/31-sdd-m9-exel-web.md).

Cada scraper vive en su propio módulo (scrapers/<code>.py), hereda de ProviderAdapter, se decora
con @register de apps.providers.registry y se importa aquí, para que quede registrado cuando
ProvidersWebConfig.ready() importe este paquete.
"""
from . import exel  # noqa: F401  (M9, docs/31-sdd-m9-exel-web.md)
