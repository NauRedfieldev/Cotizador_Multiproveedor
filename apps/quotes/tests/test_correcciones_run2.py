"""Correcciones de la auditoría run-2 en cotizaciones (docs/37).

#19: todo PDF (v1, v2 o histórico) exige view_commercial_costs, como la API.
#17: una cotización ajena responde igual que un folio inexistente (404); sin rol, 403 en ambos.
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.quotes.tests.test_commercial_persistence import CommercialFixtures, commercial_line
from apps.quotes.tests.test_snapshots import partida

FOLIO_INEXISTENTE = "NOEXISTE-2026-9999"


class PdfExigeCostosTests(CommercialFixtures, TestCase):
    """Auditoría run-2 #19: el PDF v1 lleva costo de origen, proveedor y stock."""

    def setUp(self):
        super().setUp()
        self.v1 = self.draft([partida(precio="1234.5678", cantidad="3")])
        self.url = f"/cotizaciones/{self.quote.folio}/pdf/?revision={self.v1.number}"

    def test_dueno_con_rol_descarga(self):
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_dueno_con_rol_revocado_no_descarga(self):
        self.actor.groups.clear()
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_sin_rol_comercial_no_descarga(self):
        tecnico = get_user_model().objects.create_user("staff_tecnico_qa", is_staff=True)
        self.client.force_login(tecnico)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_con_rol_descarga_cotizacion_ajena(self):
        self.client.force_login(self.staff)  # Personal CCONOR + is_staff (salto de dueño por diseño)
        self.assertEqual(self.client.get(self.url).status_code, 200)


class FolioAjenoComoInexistenteTests(CommercialFixtures, TestCase):
    """Auditoría run-2 #17: 403 frente a 404 revelaba qué folios existen (son correlativos)."""

    def setUp(self):
        super().setUp()
        self.issue(self.commercial_draft([commercial_line(kind="API")]))
        U = get_user_model().objects
        self.principales = {
            "B_mismo_rol": self.other,
            "sin_rol": U.create_user("sin_rol_qa"),
            "staff_sin_rol": U.create_user("staff_sin_rol_qa", is_staff=True),
        }

    @staticmethod
    def rutas(folio):
        b = f"/api/v1/quotes/{folio}"
        return [
            ("get", f"{b}/", None), ("get", f"{b}/revisiones/1/", None), ("put", f"{b}/revisiones/1/", {}),
            ("get", f"{b}/revisiones/1/resumen/", None), ("post", f"{b}/revisiones/1/preview/", {}),
            ("post", f"{b}/revisiones/1/confirmaciones/", {}),
            ("post", f"{b}/revisiones/1/emitir/", {"edit_version": 1}),
            ("post", f"{b}/revisiones/1/nueva/", {"request_id": "00000000-0000-4000-8000-000000000000"}),
            ("post", f"{b}/revisiones/1/nueva/preview/", {}),
            ("get", f"/cotizaciones/{folio}/pdf/?revision=1", None),
            ("post", f"/api/cotizaciones/{folio}/enviar-correo/", {"email": "b@example.invalid", "revision": 1}),
        ]

    def llamar(self, metodo, url, cuerpo):
        extra = {} if cuerpo is None else {"data": json.dumps(cuerpo), "content_type": "application/json"}
        return getattr(self.client, metodo)(url, **extra).status_code

    def test_ajeno_e_inexistente_responden_igual(self):
        for nombre, usuario in self.principales.items():
            self.client.force_login(usuario)
            for (metodo, url, cuerpo), (_, falsa, _) in zip(self.rutas(self.quote.folio), self.rutas(FOLIO_INEXISTENTE)):
                with self.subTest(principal=nombre, metodo=metodo, ruta=falsa):
                    real, inexistente = self.llamar(metodo, url, cuerpo), self.llamar(metodo, falsa, cuerpo)
                    self.assertEqual(real, inexistente)
                    self.assertNotIn(real, (200, 201))

    def test_con_rol_el_ajeno_es_404_y_sin_rol_403(self):
        detalle = f"/api/v1/quotes/{self.quote.folio}/"
        self.client.force_login(self.other)
        respuesta = self.client.get(detalle)
        self.assertEqual(respuesta.status_code, 404)
        self.assertEqual(respuesta.json()["code"], "NOT_FOUND")
        self.client.force_login(self.principales["sin_rol"])
        self.assertEqual(self.client.get(detalle).status_code, 403)

    def test_el_dueno_sigue_entrando(self):
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(f"/api/v1/quotes/{self.quote.folio}/").status_code, 200)
        self.assertEqual(self.client.get(f"/cotizaciones/{self.quote.folio}/pdf/?revision=1").status_code, 200)
