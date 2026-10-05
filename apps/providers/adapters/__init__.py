"""Adaptadores concretos de proveedores (SDD M2 §6.1).

Cada adaptador vive en su propio módulo (adapters/<code>.py), se decora con @register y se
importa aquí, para que quede registrado cuando ProvidersConfig.ready() importe este paquete.
El primero llegará con M3 (SYSCOM): `from . import syscom  # noqa: F401`.
"""
