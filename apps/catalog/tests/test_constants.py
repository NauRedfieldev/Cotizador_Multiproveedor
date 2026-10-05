from django.test import SimpleTestCase

from apps.catalog.constants import (
    EstadoMatch,
    MetodoMatch,
    UMBRAL_AUTO_MATCH,
    UMBRAL_REVISION_MANUAL,
)


class ConstantsTests(SimpleTestCase):
    def test_choices_estables(self):
        self.assertEqual(EstadoMatch.values, ["AUTO_MATCH", "REVISION_MANUAL", "SIN_MATCH"])
        self.assertEqual(MetodoMatch.values, [
            "DETERMINISTICO_MPN", "DETERMINISTICO_SKU", "DETERMINISTICO_MODELO",
        ])

    def test_umbrales_provisionales(self):
        self.assertEqual(UMBRAL_AUTO_MATCH, 90)
        self.assertEqual(UMBRAL_REVISION_MANUAL, 70)
