"""Contrato estable para el matching posterior; no implementa asociaciones."""

from django.db import models


class EstadoMatch(models.TextChoices):
    AUTO_MATCH = "AUTO_MATCH", "Coincidencia automática"
    REVISION_MANUAL = "REVISION_MANUAL", "Revisión manual"
    SIN_MATCH = "SIN_MATCH", "Sin coincidencia"


class MetodoMatch(models.TextChoices):
    DETERMINISTICO_MPN = "DETERMINISTICO_MPN", "MPN exacto"
    DETERMINISTICO_SKU = "DETERMINISTICO_SKU", "SKU de fabricante exacto"
    DETERMINISTICO_MODELO = "DETERMINISTICO_MODELO", "Marca y modelo exactos"


# PROVISIONALES: deben calibrarse con pares reales antes de validar el matching.
UMBRAL_AUTO_MATCH = 90
UMBRAL_REVISION_MANUAL = 70
