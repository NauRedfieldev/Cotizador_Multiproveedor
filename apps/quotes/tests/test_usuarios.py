"""B6: «Usuarios y permisos», invitaciones y recuperar contraseña (lienzo fila 12, docs/28 §14.19)."""

import json
import re

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.quotes.permissions import (
    ADMIN_GROUP, ASESOR_GROUP, DISCOUNT, FX, INTERNAL_GROUP, MANAGE, MANUAL_SALE, MARGIN, TAX, VIEW_COSTS,
    configure_commercial_groups,
)

LOCMEM = {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}
DUMMY = {"BACKEND": "django.core.mail.backends.dummy.EmailBackend"}
# Solo «sistemas» llega al buzón de pruebas: así se comprueba que no se usa la cuenta de ventas.
CORREO = override_settings(MAILERS={"default": DUMMY, "torva": DUMMY, "sistemas": LOCMEM},
                           SISTEMAS_SMTP_FROM="sistemas@qa.example.invalid")
ENLACE = re.compile(r"https?://[^/\s]+(/elegir-contrasena/[^\s\"<]+/)")


@CORREO
class UsuariosBase(TestCase):
    def setUp(self):
        cache.clear()
        modelo = get_user_model()
        self.root = modelo.objects.create_superuser("dueno", "dueno@qa.example.invalid", "Dueno-Qa-2026!")
        configure_commercial_groups(actor=self.root)
        self.admin = self.usuario("admin1", ADMIN_GROUP, first_name="Ana", last_name="Admin")
        self.asesor = self.usuario("asesor1", ASESOR_GROUP, first_name="Carlos", last_name="Ruiz")
        self.personal = self.usuario("personal1", INTERNAL_GROUP, first_name="Pepe", last_name="Pérez")
        self.client.force_login(self.admin)

    def usuario(self, username, grupo=None, **campos):
        nuevo = get_user_model().objects.create_user(username, f"{username}@qa.example.invalid", "Clave-Qa-2026!", **campos)
        if grupo:
            nuevo.groups.add(Group.objects.get(name=grupo))
        return nuevo

    def api(self, metodo, ruta, datos=None):
        return getattr(self.client, metodo)(ruta, data=json.dumps(datos) if datos is not None else None,
                                            content_type="application/json")

    def patch(self, usuario, datos):
        return self.api("patch", reverse("api_usuario", args=[usuario.pk]), datos)

    def fila(self, usuario):
        respuesta = self.client.get(reverse("api_usuarios"))
        self.assertEqual(respuesta.status_code, 200)
        return next(u for u in respuesta.json()["data"]["users"] if u["id"] == usuario.pk)

    def grupos(self, usuario):
        return set(usuario.groups.values_list("name", flat=True))

    def perms(self, usuario):
        fresco = get_user_model().objects.get(pk=usuario.pk)
        return {p for p in (VIEW_COSTS, FX, MANUAL_SALE, MARGIN, TAX, DISCOUNT, MANAGE) if fresco.has_perm("quotes." + p)}


class RolesTests(UsuariosBase):
    def test_grupo_asesor_con_dolares_y_manuales(self):
        self.assertEqual(self.perms(self.asesor), {VIEW_COSTS, FX, MANUAL_SALE})
        self.assertEqual(self.perms(self.personal), {VIEW_COSTS})
        self.assertEqual(self.perms(self.admin), {VIEW_COSTS, FX, MANUAL_SALE, MARGIN, TAX, DISCOUNT, MANAGE})

    def test_pagina_y_menu_solo_para_quien_gestiona(self):
        self.assertContains(self.client.get(reverse("administracion_usuarios")), "Usuarios y permisos")
        self.assertContains(self.client.get(reverse("inicio")), reverse("administracion_usuarios"))
        self.client.force_login(self.asesor)
        self.assertEqual(self.client.get(reverse("administracion_usuarios")).status_code, 403)
        self.assertNotContains(self.client.get(reverse("inicio")), reverse("administracion_usuarios"))
        self.assertEqual(self.client.get(reverse("api_usuarios")).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("administracion_usuarios")).status_code, 302)
        self.assertEqual(self.client.get(reverse("api_usuarios")).status_code, 401)

    def test_listado_con_dueno_tu_y_conteos(self):
        datos = self.client.get(reverse("api_usuarios")).json()["data"]
        filas = {u["username"]: u for u in datos["users"]}
        self.assertEqual(filas["dueno"]["role"], "owner")
        self.assertTrue(filas["admin1"]["me"])
        self.assertEqual(filas["asesor1"]["role"], "asesor")
        self.assertEqual(datos["users"][0]["username"], "dueno")
        conteos = {r["key"]: r["count"] for r in datos["roles"]}
        self.assertEqual(conteos, {"personal": 1, "asesor": 1, "admin": 1, "owner": 1})

    def test_pendiente_solo_si_nunca_entro_y_nunca_el_dueno(self):
        from django.utils import timezone
        sin_clave = self.usuario("sinclave1", INTERNAL_GROUP)
        sin_clave.set_unusable_password()
        sin_clave.save()
        self.root.set_unusable_password()
        self.root.save()
        self.assertTrue(self.fila(sin_clave)["pending"])
        self.assertFalse(self.fila(self.root)["pending"])
        get_user_model().objects.filter(pk=sin_clave.pk).update(last_login=timezone.now())
        self.assertFalse(self.fila(sin_clave)["pending"])
        self.assertEqual(self.api("post", reverse("api_reenviar_invitacion", args=[sin_clave.pk])).status_code, 400)

    def test_cambiar_rol_deja_un_solo_grupo_y_queda_registrado(self):
        respuesta = self.patch(self.personal, {"role": "asesor"})
        self.assertEqual(respuesta.status_code, 200, respuesta.content)
        self.assertEqual(self.grupos(self.personal), {ASESOR_GROUP})
        self.assertEqual(self.perms(self.personal), {VIEW_COSTS, FX, MANUAL_SALE})
        registro = LogEntry.objects.filter(object_id=str(self.personal.pk)).latest("action_time")
        self.assertEqual(registro.user_id, self.admin.pk)
        self.assertIn("rol Personal → Asesor", registro.change_message)
        self.patch(self.personal, {"role": "none"})
        self.assertEqual(self.grupos(self.personal), set())
        self.assertEqual(self.perms(self.personal), set())

    def test_extras_solo_suman_y_se_ven_como_etiqueta(self):
        self.assertEqual(self.patch(self.asesor, {"extras": [DISCOUNT]}).status_code, 200)
        fila = self.fila(self.asesor)
        self.assertEqual((fila["role"], fila["extras"], fila["extras_label"]), ("asesor", [DISCOUNT], "descuentos"))
        self.assertIn(DISCOUNT, self.perms(self.asesor))
        for extra in (FX, MANAGE, VIEW_COSTS, "auth.change_user"):
            with self.subTest(extra=extra):
                self.assertEqual(self.patch(self.asesor, {"extras": [extra]}).status_code, 400)
        self.assertEqual(self.patch(self.admin_otro(), {"extras": [DISCOUNT]}).status_code, 400)

    def admin_otro(self):
        return self.usuario("admin2", ADMIN_GROUP)

    def test_cambio_de_rol_pregunta_si_conservar_extras(self):
        self.patch(self.asesor, {"extras": [DISCOUNT]})
        respuesta = self.patch(self.asesor, {"role": "personal"})
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("conservan", respuesta.json()["error"])
        fila = self.patch(self.asesor, {"role": "personal", "keep_extras": True}).json()["data"]
        self.assertEqual((fila["role"], fila["extras"]), ("personal", [DISCOUNT]))
        self.assertEqual(self.perms(self.asesor), {VIEW_COSTS, DISCOUNT})
        fila = self.patch(self.asesor, {"role": "asesor", "keep_extras": False}).json()["data"]
        self.assertEqual(fila["extras"], [])
        self.assertEqual(self.perms(self.asesor), {VIEW_COSTS, FX, MANUAL_SALE})

    def test_a_administrador_o_sin_acceso_los_extras_desaparecen(self):
        self.patch(self.personal, {"extras": [FX, DISCOUNT]})
        self.assertEqual(self.patch(self.personal, {"role": "admin"}).json()["data"]["extras"], [])
        self.assertFalse(get_user_model().objects.get(pk=self.personal.pk).user_permissions.exists())
        self.patch(self.asesor, {"extras": [TAX]})
        self.assertEqual(self.patch(self.asesor, {"role": "none"}).json()["data"]["extras"], [])
        self.assertEqual(self.perms(self.asesor), set())

    def test_dueno_y_uno_mismo_no_se_editan(self):
        respuesta = self.patch(self.root, {"role": "personal"})
        self.assertEqual(respuesta.status_code, 403)
        self.assertIn("Dueño", respuesta.json()["error"])
        self.assertEqual(self.patch(self.root, {"is_active": False}).status_code, 403)
        self.assertEqual(self.patch(self.admin, {"role": "personal"}).status_code, 403)
        self.assertEqual(self.patch(self.admin, {"is_active": False}).status_code, 403)
        self.assertTrue(get_user_model().objects.get(pk=self.root.pk).is_superuser)

    def test_administrador_puede_nombrar_y_quitar_a_otro_administrador(self):
        self.assertEqual(self.patch(self.asesor, {"role": "admin"}).status_code, 200)
        self.client.force_login(self.asesor)
        self.assertEqual(self.patch(self.admin, {"role": "asesor"}).status_code, 200)
        self.assertEqual(self.grupos(self.admin), {ASESOR_GROUP})

    def test_no_se_queda_sin_administradores(self):
        self.client.force_login(self.root)
        for datos in ({"role": "asesor"}, {"is_active": False}):
            with self.subTest(datos=datos):
                respuesta = self.patch(self.admin, datos)
                self.assertEqual(respuesta.status_code, 400)
                self.assertIn("único administrador", respuesta.json()["error"])
        self.admin_otro()
        self.assertEqual(self.patch(self.admin, {"role": "asesor"}).status_code, 200)

    def test_desactivar_cierra_la_sesion_y_se_puede_reactivar(self):
        otro = self.client_class()
        otro.force_login(self.asesor)
        self.assertEqual(otro.get(reverse("api_capacidades_comerciales")).status_code, 200)
        fila = self.patch(self.asesor, {"is_active": False}).json()["data"]
        self.assertFalse(fila["is_active"])
        self.assertEqual(otro.get(reverse("api_capacidades_comerciales")).status_code, 401)
        self.assertTrue(self.patch(self.asesor, {"is_active": True}).json()["data"]["is_active"])
        # Reactivar no revive la sesión anterior: tiene que volver a entrar.
        self.assertEqual(otro.get(reverse("api_capacidades_comerciales")).status_code, 401)
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.asesor.pk), change_message__contains="cuenta desactivada").exists())

    def test_entradas_invalidas(self):
        for datos in ({}, {"role": "owner"}, {"role": "root"}, {"is_active": "no"}, {"extras": "descuentos"},
                      {"extras": [DISCOUNT, DISCOUNT]}, {"keep_extras": 1}, {"is_superuser": True}):
            with self.subTest(datos=datos):
                self.assertEqual(self.patch(self.personal, datos).status_code, 400)
        self.assertEqual(self.patch(get_user_model()(pk=999999), {"role": "asesor"}).status_code, 404)

    def test_sin_grupo_asesor_avisa_sin_cambiar_nada(self):
        Group.objects.filter(name=ASESOR_GROUP).delete()
        respuesta = self.patch(self.personal, {"role": "asesor"})
        self.assertEqual(respuesta.status_code, 503)
        self.assertEqual(self.grupos(self.personal), {INTERNAL_GROUP})


class InvitacionTests(UsuariosBase):
    datos = {"first_name": "Luis", "last_name": "Pérez", "username": "lperez", "email": "luis@qa.example.invalid", "role": "asesor"}

    def invitar(self, **cambios):
        return self.api("post", reverse("api_usuarios"), {**self.datos, **cambios})

    def enlace(self, mensaje):
        return ENLACE.search(mensaje.body).group(1)

    def test_invitar_crea_cuenta_sin_contrasena_y_envia_desde_sistemas(self):
        respuesta = self.invitar()
        self.assertEqual(respuesta.status_code, 201, respuesta.content)
        self.assertEqual(respuesta.json()["data"]["email"], "console")  # locmem cuenta como «no SMTP real»
        nuevo = get_user_model().objects.get(username="lperez")
        self.assertFalse(nuevo.has_usable_password())
        self.assertFalse(nuevo.is_staff or nuevo.is_superuser)
        self.assertEqual(self.grupos(nuevo), {ASESOR_GROUP})
        self.assertEqual(len(mail.outbox), 1)
        mensaje = mail.outbox[0]
        self.assertEqual(mensaje.to, ["luis@qa.example.invalid"])
        self.assertIn("sistemas@qa.example.invalid", mensaje.from_email)
        self.assertIn("Tu acceso al Portal de Cotización", mensaje.subject)
        self.assertIn("lperez", mensaje.body)
        self.assertIn("Ana Admin te dio acceso", mensaje.body)
        self.assertIn("Asesor", mensaje.body)
        self.assertIn(self.enlace(mensaje), mensaje.alternatives[0][0])
        fila = self.fila(nuevo)
        self.assertTrue(fila["pending"])
        self.assertIsNotNone(fila["invited_at"])

    def test_el_enlace_deja_elegir_contrasena_una_sola_vez_y_entra(self):
        self.invitar()
        enlace = self.enlace(mail.outbox[0])
        self.client.logout()
        respuesta = self.client.get(enlace)
        self.assertEqual(respuesta.status_code, 302)
        formulario = respuesta["Location"]
        self.assertContains(self.client.get(formulario), "Elige tu contraseña")
        corta = self.client.post(formulario, {"new_password1": "abc", "new_password2": "abc"})
        self.assertContains(corta, "demasiado corta")
        respuesta = self.client.post(formulario, {"new_password1": "Cotiza-Segura-71", "new_password2": "Cotiza-Segura-71"})
        self.assertRedirects(respuesta, reverse("inicio"), fetch_redirect_response=False)
        nuevo = get_user_model().objects.get(username="lperez")
        self.assertTrue(nuevo.check_password("Cotiza-Segura-71"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), nuevo.pk)
        self.client.logout()
        self.assertContains(self.client.get(enlace, follow=True), "Este enlace ya no sirve")
        self.assertTrue(LogEntry.objects.filter(object_id=str(nuevo.pk), change_message__contains="eligió su contraseña").exists())

    def test_reenviar_invalida_el_enlace_anterior(self):
        self.invitar()
        nuevo = get_user_model().objects.get(username="lperez")
        viejo = self.enlace(mail.outbox[0])
        respuesta = self.api("post", reverse("api_reenviar_invitacion", args=[nuevo.pk]))
        self.assertEqual(respuesta.status_code, 200, respuesta.content)
        self.assertEqual(len(mail.outbox), 2)
        self.client.logout()
        self.assertContains(self.client.get(viejo, follow=True), "Este enlace ya no sirve")
        self.assertEqual(self.client.get(self.enlace(mail.outbox[1])).status_code, 302)

    def test_reenviar_solo_a_invitaciones_pendientes(self):
        respuesta = self.api("post", reverse("api_reenviar_invitacion", args=[self.asesor.pk]))
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(self.api("post", reverse("api_reenviar_invitacion", args=[self.root.pk])).status_code, 403)

    def test_validaciones_de_la_invitacion(self):
        casos = [({"username": "ASESOR1"}, "ya existe"), ({"email": "ASESOR1@qa.example.invalid"}, "correo"),
                 ({"email": "no-es-correo"}, "correo válido"), ({"role": "owner"}, "rol"), ({"role": "none"}, "rol"),
                 ({"first_name": " "}, "nombre"), ({"username": "con espacio"}, "usuario")]
        for cambios, texto in casos:
            with self.subTest(cambios=cambios):
                respuesta = self.invitar(**cambios)
                self.assertEqual(respuesta.status_code, 400)
                self.assertIn(texto, respuesta.json()["error"])
        self.assertEqual(self.api("post", reverse("api_usuarios"), {**self.datos, "is_staff": True}).status_code, 400)
        self.assertFalse(get_user_model().objects.filter(username="lperez").exists())
        self.assertEqual(mail.outbox, [])

    def test_solo_quien_gestiona_invita(self):
        self.client.force_login(self.asesor)
        self.assertEqual(self.invitar().status_code, 403)
        self.assertFalse(get_user_model().objects.filter(username="lperez").exists())

    @override_settings(MAILERS={"default": DUMMY, "sistemas": {"BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                                                                 "OPTIONS": {"host": "smtp.qa.example.invalid", "username": ""}}},
                       SISTEMAS_SMTP_FROM="")
    def test_sin_remitente_de_sistemas_no_crea_la_cuenta(self):
        respuesta = self.invitar()
        self.assertEqual(respuesta.status_code, 503)
        self.assertIn("SISTEMAS_SMTP_FROM", respuesta.json()["error"])
        self.assertFalse(get_user_model().objects.filter(username="lperez").exists())


class ContrasenaTests(UsuariosBase):
    def pedir(self, correo):
        return self.client.post(reverse("recuperar_contrasena"), {"email": correo})

    def test_login_tiene_el_enlace_olvidaste(self):
        self.client.logout()
        self.assertContains(self.client.get(reverse("login")), reverse("recuperar_contrasena"))

    def test_mismo_mensaje_exista_o_no_el_correo(self):
        self.client.logout()
        for correo in ("asesor1@qa.example.invalid", "nadie@qa.example.invalid"):
            with self.subTest(correo=correo):
                respuesta = self.pedir(correo)
                self.assertRedirects(respuesta, reverse("recuperar_contrasena") + "?enviado=1")
        self.assertContains(self.client.get(reverse("recuperar_contrasena") + "?enviado=1"), "Revisa tu correo")
        self.assertEqual(len(mail.outbox), 1)
        mensaje = mail.outbox[0]
        self.assertEqual(mensaje.to, ["asesor1@qa.example.invalid"])
        self.assertIn("Restablece tu contraseña", mensaje.subject)
        self.assertNotIn("Clave-Qa-2026!", mensaje.body)

    def test_restablecer_cambia_la_contrasena_y_cierra_otras_sesiones(self):
        otra = self.client_class()
        otra.force_login(self.asesor)
        self.client.logout()
        self.pedir("asesor1@qa.example.invalid")
        respuesta = self.client.get(ENLACE.search(mail.outbox[0].body).group(1))
        self.client.post(respuesta["Location"], {"new_password1": "Otra-Clave-Nueva-9", "new_password2": "Otra-Clave-Nueva-9"})
        self.assertTrue(get_user_model().objects.get(pk=self.asesor.pk).check_password("Otra-Clave-Nueva-9"))
        self.assertEqual(otra.get(reverse("api_capacidades_comerciales")).status_code, 401)

    def test_no_envia_a_desactivados_ni_invitaciones_pendientes(self):
        self.patch(self.personal, {"is_active": False})
        pendiente = self.usuario("pendiente1")
        pendiente.set_unusable_password()
        pendiente.save()
        self.client.logout()
        self.pedir("personal1@qa.example.invalid")
        self.pedir("pendiente1@qa.example.invalid")
        self.assertEqual(mail.outbox, [])

    def test_limite_de_tres_por_hora_por_correo(self):
        self.client.logout()
        for _ in range(5):
            self.pedir("asesor1@qa.example.invalid")
        self.assertEqual(len(mail.outbox), 3)

    def test_correo_invalido_muestra_aviso(self):
        self.client.logout()
        self.assertContains(self.pedir("no-es-correo"), "Escribe un correo válido.")

    def test_enlace_de_cuenta_desactivada_no_sirve(self):
        self.client.logout()
        self.pedir("asesor1@qa.example.invalid")
        enlace = ENLACE.search(mail.outbox[0].body).group(1)
        get_user_model().objects.filter(pk=self.asesor.pk).update(is_active=False)
        self.assertContains(self.client.get(enlace, follow=True), "Este enlace ya no sirve")

    def test_enlace_desde_el_panel(self):
        respuesta = self.api("post", reverse("api_enlace_contrasena", args=[self.asesor.pk]))
        self.assertEqual(respuesta.status_code, 200, respuesta.content)
        self.assertEqual(mail.outbox[0].to, ["asesor1@qa.example.invalid"])
        self.assertIn("Restablece tu contraseña", mail.outbox[0].subject)
        self.assertEqual(self.api("post", reverse("api_enlace_contrasena", args=[self.root.pk])).status_code, 403)
        self.patch(self.personal, {"is_active": False})
        self.assertEqual(self.api("post", reverse("api_enlace_contrasena", args=[self.personal.pk])).status_code, 400)
        self.client.force_login(self.asesor)
        self.assertEqual(self.api("post", reverse("api_enlace_contrasena", args=[self.personal.pk])).status_code, 403)
