"""Adaptadores concretos de proveedores (SDD M2 §6.1).

Cada adaptador vive en su propio módulo (adapters/<code>.py), se decora con @register y se
importa aquí, para que quede registrado cuando ProvidersConfig.ready() importe este paquete.
"""
from . import syscom  # noqa: F401  (M3, docs/16-sdd-m3-syscom.md)
