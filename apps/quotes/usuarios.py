"""B6: usuarios y roles comerciales desde el portal (lienzo fila 12, docs/28 §14.19).

- Roles fijos: Personal, Asesor y Administrador (grupos de permissions.ROLE_PERMISSIONS); uno por persona.
- Extras: permisos sueltos que solo suman (ajustes que el rol no trae). «Gestionar usuarios» nunca es extra.
- El superusuario es el «Dueño»: nadie lo edita desde aquí; se asigna o se quita en el panel de Django.
- Altas por invitación: la persona elige su contraseña con un enlace de un solo uso. Invitaciones y enlaces
  salen de la cuenta «sistemas» (SISTEMAS_SMTP_*), nunca de la de ventas.
- Cada cambio queda en el historial de Django (LogEntry) con quién y cuándo; no hay tablas nuevas.
"""

import hashlib
import logging
from datetime import timedelta
from email.utils import formataddr
from functools import wraps
from importlib import import_module

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.tokens import default_token_generator
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Max, Prefetch
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone, translation
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .api import _EntradaInvalida, _leer_json, sesion_requerida
from .pdf import colores_marca
from .permissions import (
    ADJUSTMENTS, ADMIN_GROUP, ASESOR_GROUP, DISCOUNT, FX, INTERNAL_GROUP, MANAGE, MANUAL_SALE, MARGIN,
    ROLE_PERMISSIONS, TAX, VIEW_COSTS, require_commercial_permission,
)

logger = logging.getLogger(__name__)

GRUPO_DE_ROL = {"personal": INTERNAL_GROUP, "asesor": ASESOR_GROUP, "admin": ADMIN_GROUP}
SIN_ACCESO, DUENO = "none", "owner"
NOMBRE_ROL = {SIN_ACCESO: "Sin acceso", "personal": "Personal", "asesor": "Asesor",
              "admin": "Administrador", DUENO: "Dueño"}
ORDEN_EXTRAS = (FX, MANUAL_SALE, MARGIN, TAX, DISCOUNT)
NOMBRE_EXTRA = {FX: "tipo de cambio", MANUAL_SALE: "manuales", MARGIN: "margen", TAX: "IVA", DISCOUNT: "descuentos"}
TODOS = ADJUSTMENTS | {VIEW_COSTS, MANAGE}
PREFIJO_LOG = "B6: "
LOG_INVITACION = PREFIJO_LOG + "invitación"
# «¿Olvidaste tu contraseña?»: máximo 3 enlaces por hora para un mismo correo (y 10 por IP).
LIMITE_POR_CORREO, LIMITE_POR_IP, VENTANA_RECUPERAR = 3, 10, 3600


class _RolesSinConfigurar(Exception):
    """503: falta un grupo de rol en la BD (configure_commercial_groups no se ha vuelto a ejecutar)."""


class _RemitenteSistemasSinConfigurar(Exception):
    """503: hay servidor SMTP de «sistemas» pero no remitente."""


# ---------------------------------------------------------------------------
# Lectura de roles
# ---------------------------------------------------------------------------

def _rol(usuario, grupos):
    if usuario.is_superuser:
        return DUENO
    for clave in ("admin", "asesor", "personal"):
        if GRUPO_DE_ROL[clave] in grupos:
            return clave
    return SIN_ACCESO


def _permisos_del_rol(rol):
    if rol == DUENO:
        return TODOS
    return ROLE_PERMISSIONS.get(GRUPO_DE_ROL.get(rol), frozenset())


def _extras_permitidos(rol):
    """Solo Personal y Asesor llevan extras: Administrador ya los trae y «Sin acceso» no ve costos."""
    return ADJUSTMENTS - _permisos_del_rol(rol) if rol in ("personal", "asesor") else frozenset()


def _extras(rol, directos):
    if rol == DUENO:
        return []
    return [c for c in ORDEN_EXTRAS if c in directos and c not in _permisos_del_rol(rol)]


def _permisos_comerciales():
    return Permission.objects.filter(content_type__app_label="quotes", content_type__model="quoterevision")


def _directos(usuario):
    return {p.codename for p in usuario.user_permissions.all() if p.codename in TODOS}


def _nombre(usuario):
    return usuario.get_full_name().strip() or usuario.get_username()


def _iniciales(usuario):
    if usuario.first_name:
        return (usuario.first_name[:1] + usuario.last_name[:1]).upper()
    return usuario.get_username()[:2].upper()


def _pendiente(usuario):
    """Invitación pendiente: nunca ha entrado ni ha elegido contraseña. El Dueño nunca lo está."""
    return not usuario.is_superuser and usuario.last_login is None and not usuario.has_usable_password()


def _fila(usuario, gestor, invitado=None):
    grupos = {g.name for g in usuario.groups.all()}
    rol = _rol(usuario, grupos)
    extras = _extras(rol, _directos(usuario))
    return {
        "id": usuario.pk, "username": usuario.get_username(), "name": _nombre(usuario),
        "initials": _iniciales(usuario), "email": usuario.email, "role": rol, "role_name": NOMBRE_ROL[rol],
        "extras": extras, "extras_label": ", ".join(NOMBRE_EXTRA[c] for c in extras),
        "is_active": usuario.is_active, "pending": _pendiente(usuario),
        "invited_at": invitado.isoformat() if invitado else None,
        "last_login": usuario.last_login.isoformat() if usuario.last_login else None,
        "me": usuario.pk == gestor.pk, "owner": rol == DUENO,
    }


def listar(actor):
    gestor = require_commercial_permission(actor, MANAGE)
    usuarios = list(get_user_model().objects.prefetch_related(
        "groups", Prefetch("user_permissions", queryset=_permisos_comerciales()),
    ).order_by("-is_superuser", "first_name", "last_name", "username"))
    pendientes = [str(u.pk) for u in usuarios if _pendiente(u)]
    invitados = dict(LogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(get_user_model()), object_id__in=pendientes,
        change_message__startswith=LOG_INVITACION,
    ).values_list("object_id").annotate(Max("action_time"))) if pendientes else {}
    filas = [_fila(u, gestor, invitados.get(str(u.pk))) for u in usuarios]
    roles = [{"key": clave, "name": NOMBRE_ROL[clave], "permissions": sorted(_permisos_del_rol(clave)),
              "count": sum(1 for f in filas if f["role"] == clave)}
             for clave in ("personal", "asesor", "admin", DUENO)]
    return {"me": gestor.pk, "users": filas, "roles": roles, "extras": list(ORDEN_EXTRAS)}


# ---------------------------------------------------------------------------
# Cambios
# ---------------------------------------------------------------------------

def _grupo(rol):
    try:
        return Group.objects.get(name=GRUPO_DE_ROL[rol])
    except Group.DoesNotExist:
        raise _RolesSinConfigurar() from None


def _registrar(autor, objetivo, accion, mensaje):
    LogEntry.objects.log_actions(autor.pk, [objetivo], accion, change_message=PREFIJO_LOG + mensaje)


def _puede_editar(gestor, objetivo):
    if objetivo.is_superuser:
        raise PermissionDenied("El Dueño no se edita desde aquí.")
    if objetivo.pk == gestor.pk:
        raise PermissionDenied("No puedes cambiar tu propio rol ni desactivar tu cuenta.")


def _hay_otro_administrador(objetivo):
    return get_user_model().objects.filter(
        groups__name=ADMIN_GROUP, is_active=True, is_superuser=False,
    ).exclude(pk=objetivo.pk).exists()


def _cerrar_sesiones(usuario):
    """Borra sus sesiones guardadas: si la cuenta se reactiva, las sesiones anteriores no vuelven a servir."""
    motor = settings.SESSION_ENGINE
    if motor not in ("django.contrib.sessions.backends.db", "django.contrib.sessions.backends.cached_db"):
        return
    from django.contrib.sessions.models import Session
    tienda = import_module(motor).SessionStore
    clave = str(usuario.pk)
    for sesion in Session.objects.filter(expire_date__gte=timezone.now()).iterator():
        if sesion.get_decoded().get("_auth_user_id") == clave:
            tienda(session_key=sesion.session_key).delete()


def _bloquear_y_cargar(actor, user_id):
    """Dentro de una transacción: serializa los cambios de rol (regla del último administrador)."""
    list(Group.objects.select_for_update().filter(name=ADMIN_GROUP))
    gestor = require_commercial_permission(actor, MANAGE)
    objetivo = get_user_model().objects.select_for_update().get(pk=user_id)
    return gestor, objetivo


def actualizar(actor, user_id, datos):
    """PATCH {role?, extras?, keep_extras?, is_active?}: rol, permisos extra y cuenta activa."""
    if "role" in datos and datos["role"] not in (*GRUPO_DE_ROL, SIN_ACCESO):
        raise _EntradaInvalida("Rol no válido.")
    if "is_active" in datos and type(datos["is_active"]) is not bool:
        raise _EntradaInvalida("is_active debe ser true o false.")
    if "keep_extras" in datos and type(datos["keep_extras"]) is not bool:
        raise _EntradaInvalida("keep_extras debe ser true o false.")
    if "extras" in datos:
        extras = datos["extras"]
        if not isinstance(extras, list) or any(not isinstance(c, str) for c in extras) or len(set(extras)) != len(extras):
            raise _EntradaInvalida("extras debe ser una lista de permisos sin repetir.")
    with transaction.atomic():
        gestor, objetivo = _bloquear_y_cargar(actor, user_id)
        _puede_editar(gestor, objetivo)
        rol_actual = _rol(objetivo, set(objetivo.groups.values_list("name", flat=True)))
        directos = _directos(objetivo)
        extras_actuales = set(_extras(rol_actual, directos))
        rol = datos.get("role", rol_actual)
        activo = datos.get("is_active", objetivo.is_active)
        if rol_actual == "admin" and objetivo.is_active and (rol != "admin" or not activo) \
                and not _hay_otro_administrador(objetivo):
            raise ValidationError("Es el único administrador: nombra a otro antes de quitarle el rol o desactivarlo.")
        permitidos = _extras_permitidos(rol)
        if "extras" in datos:
            nuevos = set(datos["extras"])
            if nuevos - permitidos:
                raise ValidationError("Ese permiso ya viene con el rol o no se puede dar como extra.")
        elif rol != rol_actual:
            sobrantes = extras_actuales & permitidos
            if sobrantes and "keep_extras" not in datos:
                raise ValidationError("Indica si se conservan o se quitan sus permisos extra.")
            nuevos = sobrantes if datos.get("keep_extras") else set()
        else:
            nuevos = extras_actuales
        cambios = []
        if rol != rol_actual:
            objetivo.groups.remove(*Group.objects.filter(name__in=GRUPO_DE_ROL.values()))
            if rol != SIN_ACCESO:
                objetivo.groups.add(_grupo(rol))
            cambios.append(f"rol {NOMBRE_ROL[rol_actual]} → {NOMBRE_ROL[rol]}")
        if "role" in datos or "extras" in datos:
            # El rol es la fuente de verdad: fuera permisos directos que no sean extras vigentes.
            quitar, poner = directos - nuevos, nuevos - directos
            if quitar or poner:
                permisos = {p.codename: p for p in _permisos_comerciales().filter(codename__in=quitar | poner)}
                objetivo.user_permissions.remove(*(permisos[c] for c in quitar))
                objetivo.user_permissions.add(*(permisos[c] for c in poner))
            if nuevos != extras_actuales:
                texto = ", ".join(NOMBRE_EXTRA[c] for c in ORDEN_EXTRAS if c in nuevos) or "ninguno"
                cambios.append(f"extras: {texto}")
        if activo != objetivo.is_active:
            objetivo.is_active = activo
            objetivo.save(update_fields=["is_active"])
            if not activo:
                _cerrar_sesiones(objetivo)
            cambios.append("cuenta activada" if activo else "cuenta desactivada")
        if cambios:
            _registrar(gestor, objetivo, CHANGE, "; ".join(cambios))
        objetivo = get_user_model().objects.prefetch_related(
            "groups", Prefetch("user_permissions", queryset=_permisos_comerciales())).get(pk=objetivo.pk)
        return _fila(objetivo, gestor)


def _texto(valor, campo, limite, requerido=True):
    if not isinstance(valor, str):
        raise _EntradaInvalida(f"Falta {campo}.")
    valor = valor.strip()
    if requerido and not valor:
        raise _EntradaInvalida(f"Falta {campo}.")
    if len(valor) > limite:
        raise _EntradaInvalida(f"{campo[:1].upper()}{campo[1:]}: máximo {limite} caracteres.")
    return valor


def invitar(actor, datos, request):
    """POST {first_name, last_name, username, email, role}: cuenta sin contraseña + correo de invitación."""
    modelo = get_user_model()
    nombre = _texto(datos.get("first_name"), "el nombre", 150)
    apellidos = _texto(datos.get("last_name"), "los apellidos", 150, requerido=False)
    usuario = _texto(datos.get("username"), "el usuario", 150)
    correo = _texto(datos.get("email"), "el correo", 254)
    rol = datos.get("role")
    if rol not in GRUPO_DE_ROL:
        raise _EntradaInvalida("Elige el rol: Personal, Asesor o Administrador.")
    try:
        modelo.username_validator(usuario)
    except ValidationError:
        raise _EntradaInvalida("El usuario solo puede llevar letras, números y los signos @ . + - _") from None
    try:
        validate_email(correo)
    except ValidationError:
        raise _EntradaInvalida("Escribe un correo válido.") from None
    _correo_sistemas()  # sin remitente configurado no se crea la cuenta (503)
    try:
        with transaction.atomic():
            gestor = require_commercial_permission(actor, MANAGE)
            if modelo.objects.filter(username__iexact=usuario).exists():
                raise ValidationError("Ese usuario ya existe; elige otro.")
            if modelo.objects.filter(email__iexact=correo).exists():
                raise ValidationError("Ya hay una cuenta con ese correo.")
            nuevo = modelo(username=usuario, first_name=nombre, last_name=apellidos, email=correo)
            nuevo.set_unusable_password()
            nuevo.save()
            nuevo.groups.add(_grupo(rol))
            _registrar(gestor, nuevo, ADDITION, f"invitación como {NOMBRE_ROL[rol]}")
    except IntegrityError:
        raise ValidationError("Ese usuario ya existe; elige otro.") from None
    estado = _enviar_acceso(nuevo, request, "invitacion", gestor)
    nuevo = modelo.objects.prefetch_related(
        "groups", Prefetch("user_permissions", queryset=_permisos_comerciales())).get(pk=nuevo.pk)
    return {"user": _fila(nuevo, gestor, timezone.now()), "email": estado}


def reenviar_invitacion(actor, user_id, request):
    _correo_sistemas()
    with transaction.atomic():
        gestor, objetivo = _bloquear_y_cargar(actor, user_id)
        _puede_editar(gestor, objetivo)
        if not _pendiente(objetivo):
            raise ValidationError("Ya eligió su contraseña; usa «Enviar enlace de contraseña».")
        if not objetivo.is_active:
            raise ValidationError("La cuenta está desactivada.")
        if not objetivo.email:
            raise ValidationError("No tiene correo registrado.")
        objetivo.set_unusable_password()  # otra contraseña inutilizable: el enlace anterior deja de servir
        objetivo.save(update_fields=["password"])
        _registrar(gestor, objetivo, CHANGE, "invitación reenviada")
    return {"email": _enviar_acceso(objetivo, request, "invitacion", gestor)}


def enviar_enlace(actor, user_id, request):
    _correo_sistemas()
    with transaction.atomic():
        gestor = require_commercial_permission(actor, MANAGE)
        objetivo = get_user_model().objects.get(pk=user_id)
        if objetivo.is_superuser and objetivo.pk != gestor.pk:
            raise PermissionDenied("El Dueño no se edita desde aquí.")
        if not objetivo.is_active:
            raise ValidationError("La cuenta está desactivada.")
        if _pendiente(objetivo):
            raise ValidationError("Aún no acepta su invitación; usa «Reenviar invitación».")
        if not objetivo.email:
            raise ValidationError("No tiene correo registrado.")
        _registrar(gestor, objetivo, CHANGE, "enlace para restablecer contraseña enviado desde el panel")
    return {"email": _enviar_acceso(objetivo, request, "restablecer", gestor)}


# ---------------------------------------------------------------------------
# Correo de acceso (invitación o restablecer), desde «sistemas»
# ---------------------------------------------------------------------------

def _correo_sistemas():
    config = settings.MAILERS.get("sistemas", {})
    real = config.get("BACKEND", "").endswith("smtp.EmailBackend")
    remitente = (settings.SISTEMAS_SMTP_FROM or config.get("OPTIONS", {}).get("username", "")).strip()
    if not remitente:
        if real:
            raise _RemitenteSistemasSinConfigurar()
        remitente = "sistemas@localhost"
    return real, remitente


def _enviar_acceso(usuario, request, tipo, invito=None):
    """Devuelve «sent» (salió por SMTP), «console» (sin SISTEMAS_SMTP_HOST) o «failed»."""
    real, remitente = _correo_sistemas()
    empresa = (settings.CCONOR_EMPRESA.get("name") or "CCONOR").strip()
    nombre, _, resto = empresa.partition(" ")
    marca = colores_marca("cconor")
    enlace = request.build_absolute_uri(reverse("elegir_contrasena", args=[
        urlsafe_base64_encode(force_bytes(usuario.pk)), default_token_generator.make_token(usuario)]))
    invitacion = tipo == "invitacion"
    rol = _rol(usuario, {g.name for g in usuario.groups.all()})
    contexto = {
        "invitacion": invitacion, "usuario": usuario, "nombre": usuario.first_name or usuario.get_username(),
        "invito": _nombre(invito) if invito and invitacion else "", "rol": NOMBRE_ROL[rol], "enlace": enlace,
        "caduca": timezone.localtime(timezone.now() + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT)),
        "empresa": empresa, "marca": {"barra": marca["color"], "subtitulo": marca["tint"], "nombre": nombre, "resto": resto},
    }
    asunto = (f"Tu acceso al Portal de Cotización de {nombre}" if invitacion
              else "Restablece tu contraseña del Portal de Cotización")
    mensaje = EmailMultiAlternatives(asunto, render_to_string("registration/correo_acceso.txt", contexto),
                                     formataddr((empresa, remitente)), [usuario.email])
    mensaje.attach_alternative(render_to_string("registration/correo_acceso.html", contexto), "text/html")
    try:
        if mensaje.send(using="sistemas") != 1:
            return "failed"
    except Exception:
        logger.exception("B6: no se pudo enviar el correo de acceso al usuario %s.", usuario.pk)
        return "failed"
    return "sent" if real else "console"


# ---------------------------------------------------------------------------
# Vistas: página del panel y API
# ---------------------------------------------------------------------------

@login_required
@ensure_csrf_cookie
def administracion_usuarios(request):
    require_commercial_permission(request.user, MANAGE)
    return render(request, "administracion/usuarios.html")


def _respuesta_de_error(exc):
    if isinstance(exc, _EntradaInvalida):
        status, mensaje = 400, str(exc)
    elif isinstance(exc, ValidationError):
        status, mensaje = 400, "; ".join(exc.messages)
    elif isinstance(exc, PermissionDenied):
        status, mensaje = 403, str(exc) or "Solo un administrador puede gestionar usuarios."
    elif isinstance(exc, ObjectDoesNotExist):
        status, mensaje = 404, "Usuario no encontrado."
    elif isinstance(exc, _RolesSinConfigurar):
        status, mensaje = 503, "Faltan los roles en la base de datos; el Dueño debe volver a ejecutar la configuración de roles."
    elif isinstance(exc, _RemitenteSistemasSinConfigurar):
        status, mensaje = 503, "Falta el remitente de la cuenta de sistemas (SISTEMAS_SMTP_FROM) en el servidor."
    else:
        logger.error("B6: fallo inesperado en la gestión de usuarios.", exc_info=exc)
        status, mensaje = 500, "No se pudo completar la operación."
    return JsonResponse({"success": False, "error": mensaje}, status=status)


def _api(metodos):
    def decorador(vista):
        @require_http_methods(metodos.split())
        @sesion_requerida
        @wraps(vista)
        async def envoltura(request, *args, **kwargs):
            try:
                return await vista(request, *args, **kwargs)
            except Exception as exc:
                return _respuesta_de_error(exc)
        return envoltura
    return decorador


def _en_hilo(funcion):
    return sync_to_async(funcion, thread_sensitive=True)


@_api("GET POST")
async def api_usuarios(request):
    actor = await request.auser()
    if request.method == "GET":
        datos = await _en_hilo(listar)(actor)
        return JsonResponse({"success": True, "data": datos}, headers={"Cache-Control": "private, no-store"})
    datos = _leer_json(request, {"first_name", "last_name", "username", "email", "role"})
    return JsonResponse({"success": True, "data": await _en_hilo(invitar)(actor, datos, request)}, status=201)


@_api("PATCH")
async def api_usuario(request, user_id):
    datos = _leer_json(request, {"role", "extras", "keep_extras", "is_active"})
    if not datos:
        raise _EntradaInvalida("Nada que cambiar.")
    return JsonResponse({"success": True, "data": await _en_hilo(actualizar)(await request.auser(), user_id, datos)})


@_api("POST")
async def api_reenviar_invitacion(request, user_id):
    return JsonResponse({"success": True, "data": await _en_hilo(reenviar_invitacion)(await request.auser(), user_id, request)})


@_api("POST")
async def api_enlace_contrasena(request, user_id):
    return JsonResponse({"success": True, "data": await _en_hilo(enviar_enlace)(await request.auser(), user_id, request)})


# ---------------------------------------------------------------------------
# «¿Olvidaste tu contraseña?» y «Elige tu contraseña»
# ---------------------------------------------------------------------------

def _dentro_del_limite(correo, ip):
    claves = [(f"recuperar:correo:{hashlib.sha256(correo.lower().encode()).hexdigest()}", LIMITE_POR_CORREO),
              (f"recuperar:ip:{ip}", LIMITE_POR_IP)]
    dentro = True
    for clave, limite in claves:
        cache.add(clave, 0, VENTANA_RECUPERAR)
        try:
            cuenta = cache.incr(clave)
        except ValueError:  # caducó entre add e incr
            cache.set(clave, 1, VENTANA_RECUPERAR)
            cuenta = 1
        dentro = dentro and cuenta <= limite
    return dentro


@require_http_methods(["GET", "POST"])
def recuperar_contrasena(request):
    """Mismo mensaje exista o no el correo: no revela quién tiene cuenta."""
    invalido = False
    if request.method == "POST":
        form = PasswordResetForm(request.POST)
        if form.is_valid():
            correo = form.cleaned_data["email"]
            if _dentro_del_limite(correo, request.META.get("REMOTE_ADDR", "")):
                # get_users: solo cuentas activas que ya eligieron contraseña (no invitaciones pendientes).
                for usuario in form.get_users(correo):
                    try:
                        _registrar(usuario, usuario, CHANGE, "enlace para restablecer contraseña pedido desde el inicio de sesión")
                        _enviar_acceso(usuario, request, "restablecer")
                    except _RemitenteSistemasSinConfigurar:
                        logger.error("B6: falta SISTEMAS_SMTP_FROM; no se envió el enlace para restablecer.")
            return HttpResponseRedirect(reverse("recuperar_contrasena") + "?enviado=1")
        invalido = True
    return render(request, "registration/recuperar_contrasena.html",
                  {"enviado": request.GET.get("enviado") == "1", "invalido": invalido})


class ElegirContrasena(auth_views.PasswordResetConfirmView):
    """Enlace de invitación o de restablecer: un solo uso, caduca en PASSWORD_RESET_TIMEOUT y entra al guardar."""

    template_name = "registration/elegir_contrasena.html"
    post_reset_login = True
    success_url = reverse_lazy("inicio")

    def get_user(self, uidb64):
        usuario = super().get_user(uidb64)
        return usuario if usuario is not None and usuario.is_active else None

    def form_valid(self, form):
        respuesta = super().form_valid(form)
        _registrar(form.user, form.user, CHANGE, "eligió su contraseña con el enlace")
        return respuesta

    def dispatch(self, *args, **kwargs):
        # Mensajes de las reglas de contraseña en español sin cambiar el idioma del resto del sitio.
        with translation.override("es"):
            respuesta = super().dispatch(*args, **kwargs)
            if hasattr(respuesta, "render") and not respuesta.is_rendered:
                respuesta.render()
        return respuesta
