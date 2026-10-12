"""Casos sintéticos para las guardas iniciales de redes."""

from django.test import SimpleTestCase

from apps.catalog.normalizer import normalizar
from apps.catalog.specs import compatibles, specs


class SpecsTests(SimpleTestCase):
    def extraer(self, texto):
        return specs(normalizar(texto))

    def test_extrae_todos_los_atributos(self):
        self.assertEqual(self.extraer("SW 24P POE+ 4X10G LAN BASE 1U"), {
            "puertos": frozenset({"24"}), "poe": frozenset({"POE+"}),
            "uplinks": frozenset({"4X10G"}), "licencia": frozenset({"LANBASE"}),
            "rack": frozenset({"1"}),
        })

    def test_poe_conserva_signos_y_no_confunde_negacion(self):
        for texto, esperado in [("POE", "POE"), ("POE+", "POE+"), ("POE++", "POE++"),
                                ("sin poe", "SIN_POE"), ("no poe", "SIN_POE")]:
            with self.subTest(texto=texto):
                self.assertEqual(self.extraer(texto)["poe"], frozenset({esperado}))
        self.assertFalse(compatibles(self.extraer("SIN POE"), self.extraer("POE+")))

    def test_rechaza_diferencias_explicitas(self):
        for a, b in [("POE", "POE+"), ("24P", "48P"), ("4X1G", "4X10G"),
                     ("LAN BASE", "LAN LITE"), ("IP BASE", "IP SERVICES"), ("1U", "2U")]:
            with self.subTest(a=a, b=b):
                self.assertFalse(compatibles(self.extraer(a), self.extraer(b)))

    def test_ausencia_no_es_contradiccion(self):
        self.assertTrue(compatibles(self.extraer("SWITCH"), self.extraer("SWITCH 24P POE+")))
        self.assertTrue(compatibles({}, {}))

    def test_licencias_y_puertos_con_espacios(self):
        self.assertTrue(compatibles(self.extraer("24P LANBASE"), self.extraer("24 puertos LAN BASE")))
        self.assertEqual(self.extraer("4 X 10G"), self.extraer("4X10G"))

    def test_limites_no_extraen_fragmentos_de_modelos(self):
        self.assertEqual(self.extraer("ABC24P ABCPOE+ MODELO4X10G RACK1U LANBASEX"), {})
        self.assertEqual(self.extraer("1/2 U"), {})

    def test_conserva_multiples_apariciones(self):
        valores = self.extraer("24P 48P POE POE+")
        self.assertEqual(valores["puertos"], frozenset({"24", "48"}))
        self.assertEqual(valores["poe"], frozenset({"POE", "POE+"}))
        self.assertFalse(compatibles(valores, {}))

    def test_repeticion_del_mismo_valor_no_es_ambigua(self):
        self.assertTrue(compatibles(self.extraer("POE+ POE+ 24P 24P"), self.extraer("POE+ 24P")))

    def test_uplinks_multiples_se_comparan_completos(self):
        a = self.extraer("4X1G 2X10G")
        self.assertTrue(compatibles(a, self.extraer("2X10G 4X1G")))
        self.assertFalse(compatibles(a, self.extraer("4X1G")))
