"""B6: «Usuarios y permisos», invitaciones y recuperar contraseña (lienzo fila 12, docs/28 §14.19)."""

import json
import re
import threading
from datetime import datetime, timedelta
from unittest import mock

from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.providers.tests.soporte import limpiar_caches
from apps.quotes.permissions import (
    ADMIN_GROUP, ASESOR_GROUP, DISCOUNT, FX, INTERNAL_GROUP, MANAGE, MANUAL_SALE, MARGIN, TAX, VIEW_COSTS,
    configure_commercial_groups,
)
from apps.quotes import usuarios
from apps.quotes.usuarios import DURACION_RESTABLECER, LIMITE_LOGIN_POR_IP, LIMITE_POR_CUENTA, LIMITE_POR_IP

LOCMEM = {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}
DUMMY = {"BACKEND": "django.core.mail.backends.dummy.EmailBackend"}
# Solo «sistemas» llega al buzón de pruebas: así se comprueba que no se usa la cuenta de ventas.
CORREO = override_settings(MAILERS={"default": DUMMY, "torva": DUMMY, "sistemas": LOCMEM},
                           SISTEMAS_SMTP_FROM="sistemas@qa.example.invalid")
ENLACE = re.compile(r"https?://[^/\s]+(/elegir-contrasena/[^\s\"<]+/)")


@CORREO
class UsuariosBase(TestCase):
    esperar_correos = True  # el enlace para restablecer sale en un hilo: las pruebas lo esperan

    def setUp(self):
        limpiar_caches()
        self.hilos_de_correo = []
        en_segundo_plano = usuarios._en_segundo_plano

        def y_esperar(*args):
            hilo = en_segundo_plano(*args)
            self.hilos_de_correo.append(hilo)
            if self.esperar_correos:
                hilo.join(10)
            return hilo

        parche = mock.patch.object(usuarios, "_en_segundo_plano", y_esperar)
        parche.start()
        self.addCleanup(parche.stop)
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

    def guardar_en_admin(self, cliente, usuario, activo):
        """POST del formulario de usuario de /admin/ con los datos actuales; solo cambia is_active."""
        usuario.refresh_from_db()
        local = lambda fecha: timezone.localtime(fecha)
        datos = {"username": usuario.username, "first_name": usuario.first_name, "last_name": usuario.last_name,
                 "email": usuario.email, "groups": list(usuario.groups.values_list("pk", flat=True)),
                 "date_joined_0": local(usuario.date_joined).strftime("%Y-%m-%d"),
                 "date_joined_1": local(usuario.date_joined).strftime("%H:%M:%S")}
        if usuario.last_login:
            datos |= {"last_login_0": local(usuario.last_login).strftime("%Y-%m-%d"),
                      "last_login_1": local(usuario.last_login).strftime("%H:%M:%S")}
        if activo:
            datos["is_active"] = "on"
        return cliente.post(reverse("admin:auth_user_change", args=[usuario.pk]), datos)

    def test_desactivar_en_admin_tambien_cierra_la_sesion(self):
        """Pista 4 de la auditoría: /admin/ cumple la misma regla que el portal."""
        otro = self.client_class()
        otro.force_login(self.asesor)
        self.assertEqual(otro.get(reverse("api_capacidades_comerciales")).status_code, 200)
        dueno = self.client_class()
        dueno.force_login(self.root)
        self.assertRedirects(self.guardar_en_admin(dueno, self.asesor, activo=False), reverse("admin:auth_user_changelist"))
        self.assertFalse(get_user_model().objects.get(pk=self.asesor.pk).is_active)
        self.assertRedirects(self.guardar_en_admin(dueno, self.asesor, activo=True), reverse("admin:auth_user_changelist"))
        self.assertTrue(get_user_model().objects.get(pk=self.asesor.pk).is_active)
        # Reactivar no revive la sesión anterior: tiene que volver a entrar.
        self.assertEqual(otro.get(reverse("api_capacidades_comerciales")).status_code, 401)

    def test_editar_en_admin_sin_desactivar_no_cierra_la_sesion(self):
        otro = self.client_class()
        otro.force_login(self.asesor)
        dueno = self.client_class()
        dueno.force_login(self.root)
        self.assertRedirects(self.guardar_en_admin(dueno, self.asesor, activo=True), reverse("admin:auth_user_changelist"))
        self.assertEqual(otro.get(reverse("api_capacidades_comerciales")).status_code, 200)

    def test_usuarios_y_grupos_en_admin_solo_para_el_superusuario(self):
        """Endurecimiento (auditoría run-1 §5): un staff con permisos de auth no ve ni edita usuarios ni grupos,
        porque desde ahí podría darse superusuario."""
        from django.contrib.auth.models import Permission
        staff = self.usuario("staff1", is_staff=True)
        staff.user_permissions.set(Permission.objects.filter(content_type__app_label="auth"))
        rutas = [reverse("admin:auth_user_changelist"), reverse("admin:auth_user_add"),
                 reverse("admin:auth_user_change", args=[self.asesor.pk]),
                 reverse("admin:auth_user_password_change", args=[self.asesor.pk]),
                 reverse("admin:auth_group_changelist"), reverse("admin:auth_group_add"),
                 reverse("admin:auth_group_change", args=[Group.objects.get(name=ADMIN_GROUP).pk])]
        cliente = self.client_class()
        cliente.force_login(staff)
        # Desde docs/37 §9 todo /admin/ es solo del Dueño: el staff va al login del admin (302), no a un 403.
        for ruta in rutas:
            with self.subTest(ruta=ruta):
                self.assertEqual(cliente.get(ruta).status_code, 302)
        datos = {"username": "staff1", "is_staff": "on", "is_active": "on", "is_superuser": "on"}
        self.assertEqual(cliente.post(reverse("admin:auth_user_change", args=[staff.pk]), datos).status_code, 302)
        self.assertFalse(get_user_model().objects.get(pk=staff.pk).is_superuser)
        self.assertEqual(cliente.get(reverse("admin:index")).status_code, 302)
        dueno = self.client_class()
        dueno.force_login(self.root)
        for ruta in rutas:
            with self.subTest(dueno=ruta):
                self.assertEqual(dueno.get(ruta).status_code, 200)

    def test_todo_el_admin_solo_para_el_dueno(self):
        """docs/37 §9: catálogo y proveedores también; un staff con todos sus permisos no entra a /admin/."""
        from django.contrib.auth.models import Permission
        staff = self.usuario("staff_catalogo", is_staff=True)
        staff.user_permissions.set(Permission.objects.filter(content_type__app_label__in=["catalog", "providers"]))
        sin_staff = self.usuario("root_sin_staff", is_superuser=True)
        rutas = [reverse("admin:index"), reverse("admin:catalog_sinonimored_changelist"),
                 reverse("admin:catalog_productocanonico_changelist"), reverse("admin:providers_provider_changelist")]
        for usuario in (staff, sin_staff):
            cliente = self.client_class()
            cliente.force_login(usuario)
            for ruta in rutas:
                with self.subTest(usuario=usuario.username, ruta=ruta):
                    respuesta = cliente.get(ruta)
                    self.assertEqual(respuesta.status_code, 302)
                    self.assertIn(reverse("admin:login"), respuesta["Location"])
        dueno = self.client_class()
        dueno.force_login(self.root)
        for ruta in rutas:
            with self.subTest(dueno=ruta):
                self.assertEqual(dueno.get(ruta).status_code, 200)

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

    def test_la_invitacion_sigue_durando_dias(self):
        """Solo restablecer se acortó a una hora: quien es nuevo puede abrir la invitación al día siguiente."""
        self.invitar()
        enlace = self.enlace(mail.outbox[0])
        self.client.logout()
        ahora = datetime.now()
        for segundos, texto in ((86400, "Elige tu contraseña"), (settings.PASSWORD_RESET_TIMEOUT + 60, "Este enlace ya no sirve")):
            with self.subTest(segundos=segundos), \
                    mock.patch.object(PasswordResetTokenGenerator, "_now", return_value=ahora + timedelta(seconds=segundos)):
                self.assertContains(self.client.get(enlace, follow=True), texto)

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
    def pedir(self, correo, ip="127.0.0.1"):
        return self.client.post(reverse("recuperar_contrasena"), {"email": correo}, REMOTE_ADDR=ip)

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

    def test_el_limite_de_la_cuenta_vale_desde_cualquier_ip(self):
        """Nadie llena el buzón de una persona cambiando de red: 3 enlaces por hora en total."""
        self.client.logout()
        for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5"):
            self.pedir("asesor1@qa.example.invalid", ip=ip)
        self.assertEqual(len(mail.outbox), LIMITE_POR_CUENTA)

    def test_una_ip_pasada_del_limite_no_gasta_los_envios_de_la_persona(self):
        """Pista 11 de la auditoría: quien ya agotó su IP no deja sin enlace a nadie."""
        self.client.logout()
        for numero in range(LIMITE_POR_IP):
            self.pedir(f"nadie{numero}@qa.example.invalid", ip="10.0.0.1")
        for _ in range(LIMITE_POR_CUENTA):
            self.pedir("asesor1@qa.example.invalid", ip="10.0.0.1")
        self.assertEqual(mail.outbox, [])
        self.pedir("asesor1@qa.example.invalid", ip="10.0.0.2")
        self.assertEqual([m.to for m in mail.outbox], [["asesor1@qa.example.invalid"]])

    def test_correos_sin_cuenta_no_gastan_envios(self):
        self.client.logout()
        for numero in range(LIMITE_POR_CUENTA + 1):
            self.pedir(f"nadie{numero}@qa.example.invalid", ip=f"10.0.0.{numero + 1}")
        self.pedir("asesor1@qa.example.invalid", ip="10.0.0.9")
        self.assertEqual(len(mail.outbox), 1)

    def test_limite_de_diez_por_ip(self):
        self.client.logout()
        for numero in range(LIMITE_POR_IP):
            self.pedir(f"nadie{numero}@qa.example.invalid", ip="10.0.0.1")
        self.pedir("asesor1@qa.example.invalid", ip="10.0.0.1")
        self.assertEqual(mail.outbox, [])
        self.pedir("asesor1@qa.example.invalid", ip="10.0.0.2")
        self.assertEqual(len(mail.outbox), 1)

    def test_un_texto_que_no_es_ip_no_da_pedidos_nuevos(self):
        """Pista 13: con un proxy que deja pasar X-Forwarded-For, inventar un texto por petición no sirve."""
        self.client.logout()
        for numero in range(LIMITE_POR_IP):
            self.pedir(f"nadie{numero}@qa.example.invalid", ip=f"k{numero}")
        self.pedir("asesor1@qa.example.invalid", ip="otro-texto")
        self.assertEqual(mail.outbox, [])
        self.pedir("asesor1@qa.example.invalid", ip="10.0.0.2")
        self.assertEqual(len(mail.outbox), 1)

    def test_la_pagina_responde_sin_esperar_al_correo(self):
        """Si la página esperara al SMTP, tardaría más con un correo que sí tiene cuenta."""
        self.client.logout()
        self.esperar_correos = False
        liberar = threading.Event()
        enviar = usuarios._enviar
        with mock.patch.object(usuarios, "_enviar", lambda *args: liberar.wait(10) and enviar(*args)):
            respuesta = self.pedir("asesor1@qa.example.invalid")
            self.assertRedirects(respuesta, reverse("recuperar_contrasena") + "?enviado=1")
            self.assertEqual(mail.outbox, [])  # la página ya respondió y el correo aún no sale
            liberar.set()
            for hilo in self.hilos_de_correo:
                hilo.join(10)
        self.assertEqual([m.to for m in mail.outbox], [["asesor1@qa.example.invalid"]])

    def test_correo_invalido_muestra_aviso(self):
        self.client.logout()
        self.assertContains(self.pedir("no-es-correo"), "Escribe un correo válido.")

    def abrir_a_los(self, enlace, segundos):
        ahora = datetime.now()
        with mock.patch.object(PasswordResetTokenGenerator, "_now", return_value=ahora + timedelta(seconds=segundos)):
            return self.client.get(enlace, follow=True)

    def test_el_enlace_para_restablecer_dura_una_hora(self):
        self.client.logout()
        self.pedir("asesor1@qa.example.invalid")
        enlace = ENLACE.search(mail.outbox[0].body).group(1)
        self.assertIn("caduca el", mail.outbox[0].body)
        self.assertContains(self.abrir_a_los(enlace, DURACION_RESTABLECER + 60), "Este enlace ya no sirve")
        self.assertContains(self.abrir_a_los(enlace, DURACION_RESTABLECER - 60), "Elige tu contraseña")

    def test_el_correo_dice_cuando_caduca(self):
        self.client.logout()
        antes = timezone.localtime()
        self.pedir("asesor1@qa.example.invalid")
        caduca = timezone.localtime(antes + timedelta(seconds=DURACION_RESTABLECER))
        posibles = {caduca.strftime("%H:%M"), (caduca + timedelta(minutes=1)).strftime("%H:%M")}  # por si cambia el minuto
        self.assertTrue(any(f"a las {hora}" in mail.outbox[0].body for hora in posibles), mail.outbox[0].body)

    def test_el_enlace_usado_ya_no_sirve(self):
        self.client.logout()
        self.pedir("asesor1@qa.example.invalid")
        enlace = ENLACE.search(mail.outbox[0].body).group(1)
        formulario = self.client.get(enlace)["Location"]
        self.client.post(formulario, {"new_password1": "Otra-Clave-Nueva-9", "new_password2": "Otra-Clave-Nueva-9"})
        self.client.logout()
        self.assertContains(self.client.get(enlace, follow=True), "Este enlace ya no sirve")

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


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])  # solo para que corra rápido
class LimiteDeLoginTests(UsuariosBase):
    """Pista 10 de la auditoría: login/ y admin/login/ tienen límite de intentos fallidos por IP."""

    def setUp(self):
        super().setUp()
        self.client.logout()

    def entrar(self, contrasena, ruta="login", ip="127.0.0.1", usuario="asesor1"):
        return self.client.post(reverse(ruta), {"username": usuario, "password": contrasena}, REMOTE_ADDR=ip)

    def fallar(self, veces, **kwargs):
        for _ in range(veces):
            self.assertContains(self.entrar("mala", **kwargs), "Usuario o contraseña incorrectos.")

    def test_al_pasar_el_limite_no_comprueba_la_contrasena(self):
        self.fallar(LIMITE_LOGIN_POR_IP)
        with mock.patch("django.contrib.auth.forms.authenticate", wraps=authenticate) as comprobar:
            for contrasena in ("mala", "Clave-Qa-2026!"):  # ni la correcta entra mientras dura el bloqueo
                with self.subTest(contrasena=contrasena):
                    respuesta = self.entrar(contrasena)
                    self.assertContains(respuesta, "Demasiados intentos fallidos")
                    self.assertNotContains(respuesta, "Usuario o contraseña incorrectos.")
                    self.assertNotIn("_auth_user_id", self.client.session)
        comprobar.assert_not_called()

    def test_los_inicios_correctos_no_gastan_intentos(self):
        self.fallar(LIMITE_LOGIN_POR_IP - 1)
        for _ in range(5):
            self.assertRedirects(self.entrar("Clave-Qa-2026!"), reverse("inicio"), fetch_redirect_response=False)
            self.client.logout()
        self.fallar(1)
        self.assertContains(self.entrar("mala"), "Demasiados intentos fallidos")

    def test_el_admin_comparte_el_limite(self):
        self.fallar(LIMITE_LOGIN_POR_IP)
        with mock.patch("django.contrib.auth.forms.authenticate", wraps=authenticate) as comprobar:
            respuesta = self.entrar("Dueno-Qa-2026!", ruta="admin:login", usuario="dueno")
        self.assertContains(respuesta, "Demasiados intentos fallidos")
        comprobar.assert_not_called()

    def test_otra_ip_no_queda_bloqueada(self):
        self.fallar(LIMITE_LOGIN_POR_IP)
        self.assertContains(self.entrar("Clave-Qa-2026!"), "Demasiados intentos fallidos")
        self.assertRedirects(self.entrar("Clave-Qa-2026!", ip="10.0.0.9"), reverse("inicio"), fetch_redirect_response=False)

    def test_ipv6_de_la_misma_red_comparte_el_limite(self):
        """Pista 13: cambiar de dirección dentro del mismo /64 no da intentos nuevos."""
        for numero in range(1, LIMITE_LOGIN_POR_IP + 1):
            self.fallar(1, ip=f"2001:db8:0:5::{numero:x}")
        self.assertContains(self.entrar("Clave-Qa-2026!", ip="2001:db8:0:5::ffff"), "Demasiados intentos fallidos")
        self.assertRedirects(self.entrar("Clave-Qa-2026!", ip="2001:db8:0:6::1"), reverse("inicio"), fetch_redirect_response=False)

    def test_llenar_la_cache_general_no_borra_el_contador(self):
        """Pista 14: las búsquedas (Exel, tipo de cambio) llenaban la misma caché de 300 y borraban los contadores."""
        self.fallar(LIMITE_LOGIN_POR_IP)
        for numero in range(400):
            cache.set(f"exel:busqueda:{numero}", "x", 300)
        self.assertContains(self.entrar("Clave-Qa-2026!"), "Demasiados intentos fallidos")

    def test_sin_contrasena_no_cuenta(self):
        for _ in range(LIMITE_LOGIN_POR_IP + 1):
            self.entrar("")
        self.assertRedirects(self.entrar("Clave-Qa-2026!"), reverse("inicio"), fetch_redirect_response=False)


class IpParaLimitesTests(TestCase):
    def test_normaliza_la_ip(self):
        casos = {
            "201.150.10.20": "201.150.10.20",
            "2001:db8:0:5::1": "2001:db8:0:5::/64",
            "2001:DB8:0:5:ffff:ffff:ffff:ffff": "2001:db8:0:5::/64",
            "::ffff:201.150.10.20": "201.150.10.20",
            "fe80::1%eth0": "fe80::/64",
            "k1": "no-es-ip",
            "201.150.10.20:5000": "no-es-ip",
            "": "no-es-ip",
        }
        for valor, clave in casos.items():
            with self.subTest(valor=valor):
                self.assertEqual(usuarios._ip_para_limites(RequestFactory().get("/", REMOTE_ADDR=valor)), clave)
        self.assertEqual(usuarios._ip_para_limites(None), "no-es-ip")


class RevocarDuranteLaValidacion:
    """Validador de prueba: corre después de leer al usuario y antes de guardar, la ventana de la carrera."""

    accion = None

    def validate(self, password, user=None):
        if RevocarDuranteLaValidacion.accion:
            accion, RevocarDuranteLaValidacion.accion = RevocarDuranteLaValidacion.accion, None
            accion()

    def get_help_text(self):
        return ""


CON_REVOCACION = override_settings(AUTH_PASSWORD_VALIDATORS=[
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": f"{__name__}.RevocarDuranteLaValidacion"},
])


@CON_REVOCACION
class RevocacionDuranteContrasenaTests(UsuariosBase):
    """Pista 3 de la auditoría: elegir/cambiar contraseña no deshace una revocación hecha a la mitad."""

    def tearDown(self):
        RevocarDuranteLaValidacion.accion = None

    def formulario_de_enlace(self, cliente, usuario):
        cliente.post(reverse("recuperar_contrasena"), {"email": usuario.email})
        return cliente.get(ENLACE.search(mail.outbox[-1].body).group(1))["Location"]

    def test_desactivar_en_el_portal_mientras_elige_contrasena(self):
        anonimo = self.client_class()
        formulario = self.formulario_de_enlace(anonimo, self.asesor)
        RevocarDuranteLaValidacion.accion = lambda: self.assertEqual(self.patch(self.asesor, {"is_active": False}).status_code, 200)
        respuesta = anonimo.post(formulario, {"new_password1": "Otra-Clave-Nueva-9", "new_password2": "Otra-Clave-Nueva-9"})
        self.assertEqual(respuesta.status_code, 403)
        fresco = get_user_model().objects.get(pk=self.asesor.pk)
        self.assertFalse(fresco.is_active)
        self.assertTrue(fresco.check_password("Clave-Qa-2026!"))
        self.assertNotIn("_auth_user_id", anonimo.session)

    def test_quitar_superusuario_mientras_elige_contrasena_no_se_revierte(self):
        jefe = self.usuario("jefe1", is_staff=True, is_superuser=True)
        anonimo = self.client_class()
        formulario = self.formulario_de_enlace(anonimo, jefe)
        quitar = lambda: get_user_model().objects.filter(pk=jefe.pk).update(is_staff=False, is_superuser=False)
        RevocarDuranteLaValidacion.accion = quitar
        respuesta = anonimo.post(formulario, {"new_password1": "Otra-Clave-Nueva-9", "new_password2": "Otra-Clave-Nueva-9"})
        self.assertRedirects(respuesta, reverse("inicio"), fetch_redirect_response=False)
        fresco = get_user_model().objects.get(pk=jefe.pk)
        self.assertTrue(fresco.check_password("Otra-Clave-Nueva-9"))
        self.assertFalse(fresco.is_staff or fresco.is_superuser)

    def cambiar_en_admin(self, cliente, nueva="Otra-Clave-Nueva-9"):
        return cliente.post(reverse("admin:password_change"), {
            "old_password": "Clave-Qa-2026!", "new_password1": nueva, "new_password2": nueva})

    def test_quitar_staff_mientras_cambia_contrasena_en_admin(self):
        # /admin/ es solo del Dueño (docs/37 §9): quitarle staff o superusuario a la mitad gana igual.
        for revocar in ({"is_staff": False}, {"is_superuser": False}):
            with self.subTest(revocar=revocar):
                jefe = self.usuario(f"jefe_{next(iter(revocar))}", is_staff=True, is_superuser=True)
                cliente = self.client_class()
                cliente.force_login(jefe)
                RevocarDuranteLaValidacion.accion = lambda: get_user_model().objects.filter(pk=jefe.pk).update(**revocar)
                self.assertEqual(self.cambiar_en_admin(cliente).status_code, 403)
                fresco = get_user_model().objects.get(pk=jefe.pk)
                self.assertTrue(fresco.check_password("Clave-Qa-2026!"))
                self.assertEqual(cliente.get(reverse("admin:index")).status_code, 302)   # el admin manda al login

    def test_sin_revocacion_el_cambio_en_admin_sigue_igual(self):
        jefe = self.usuario("jefe1", is_staff=True, is_superuser=True)
        cliente = self.client_class()
        cliente.force_login(jefe)
        self.assertRedirects(self.cambiar_en_admin(cliente), reverse("admin:password_change_done"))
        self.assertTrue(get_user_model().objects.get(pk=jefe.pk).check_password("Otra-Clave-Nueva-9"))
        self.assertEqual(cliente.get(reverse("admin:index")).status_code, 200)   # la sesión sigue viva


class VencimientoDeSesionTests(UsuariosBase):
    """Endurecimiento (auditoría run-1 §5): la sesión vence tras 8 h sin usar el sistema, no a las 2 semanas."""

    def sesion(self):
        from django.contrib.sessions.models import Session
        return Session.objects.get(session_key=self.client.session.session_key)

    def test_cada_visita_renueva_las_8_horas(self):
        sesion = self.sesion()
        sesion.expire_date = timezone.now() + timedelta(minutes=5)   # casi vencida: lleva horas sin usarse
        sesion.save()
        respuesta = self.client.get(reverse("inicio"))
        self.assertEqual(respuesta.status_code, 200)
        restante = self.sesion().expire_date - timezone.now()
        self.assertGreater(restante, timedelta(hours=7, minutes=59))
        self.assertLessEqual(restante, timedelta(hours=8))
        self.assertEqual(respuesta.cookies[settings.SESSION_COOKIE_NAME]["max-age"], 8 * 60 * 60)

    def test_sesion_vencida_pide_entrar_de_nuevo(self):
        sesion = self.sesion()
        sesion.expire_date = timezone.now() - timedelta(seconds=1)
        sesion.save()
        self.assertEqual(self.client.get(reverse("api_capacidades_comerciales")).status_code, 401)
