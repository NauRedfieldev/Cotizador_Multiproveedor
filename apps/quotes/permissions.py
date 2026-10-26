"""Capacidades comerciales, sin ampliar acceso a objetos ni privilegios técnicos.

Se consulta un usuario fresco en cada comprobación: las cachés de has_perm del
actor de una petición anterior no deben sobrevivir a una revocación.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

INTERNAL_GROUP = "Personal CCONOR"
ADMIN_GROUP = "Administrador CCONOR"
VIEW_COSTS = "view_commercial_costs"
MANAGE = "manage_commercial_permissions"
MARGIN = "change_commercial_margin"
MANUAL_SALE = "set_commercial_sale"
TAX = "change_commercial_tax"
FX = "change_commercial_fx"
DISCOUNT = "apply_commercial_discount"
ADJUSTMENTS = frozenset((MARGIN, MANUAL_SALE, TAX, FX, DISCOUNT))
ASESOR_GROUP = "Asesor CCONOR"
# B6 (escenario D): tres roles fijos. Asesor = Personal + dólares + partidas manuales/servicios.
ROLE_PERMISSIONS = {
    INTERNAL_GROUP: frozenset((VIEW_COSTS,)),
    ASESOR_GROUP: frozenset((VIEW_COSTS, FX, MANUAL_SALE)),
    ADMIN_GROUP: ADJUSTMENTS | {VIEW_COSTS, MANAGE},
}


def fresh_actor(actor):
    if not actor or not actor.is_authenticated or not actor.pk:
        raise PermissionDenied("Se requiere un usuario interno activo.")
    try:
        current = get_user_model().objects.get(pk=actor.pk)
    except get_user_model().DoesNotExist:
        raise PermissionDenied("Usuario no disponible.") from None
    if not current.is_active:
        raise PermissionDenied("Se requiere un usuario interno activo.")
    return current


def require_commercial_permission(actor, capability=VIEW_COSTS):
    current = fresh_actor(actor)
    if not current.has_perm(f"quotes.{VIEW_COSTS}") or not current.has_perm(f"quotes.{capability}"):
        raise PermissionDenied("No tienes la capacidad comercial requerida.")
    return current


def configure_commercial_groups(*, actor):
    """Bootstrap explícito, después de migrate/post_migrate; no asigna usuarios.

    Solo operación técnica confiable. No se ejecuta al importar ni durante una
    migración. El grupo comercial no recibe is_staff/is_superuser/auth.change_user.
    """
    current = fresh_actor(actor)
    if not current.is_superuser:
        raise PermissionDenied("La inicialización de roles requiere operación técnica autorizada.")
    with transaction.atomic():
        permisos = {p.codename: p for p in Permission.objects.filter(
            content_type__app_label="quotes", content_type__model="quoterevision",
            codename__in=ADJUSTMENTS | {VIEW_COSTS, MANAGE})}
        if set(permisos) != ADJUSTMENTS | {VIEW_COSTS, MANAGE}:
            raise ValidationError("Faltan permisos comerciales; completar migrate/post_migrate primero.")
        grupos = {}
        for nombre, codenames in ROLE_PERMISSIONS.items():
            grupos[nombre], _ = Group.objects.get_or_create(name=nombre)
            grupos[nombre].permissions.set([permisos[c] for c in codenames])
        return grupos[INTERNAL_GROUP], grupos[ADMIN_GROUP]


def set_commercial_permission(user_id, capability, *, actor, enabled):
    """Gestor limitado: solo cinco ajustes de un asesor ya interno.

    No añade grupos, no concede gestión/lectura interna, no cambia campos de User.
    Rechaza permisos heredados antes de fingir una revocación que no tendría efecto.
    """
    if not isinstance(capability, str) or capability not in ADJUSTMENTS or type(enabled) is not bool:
        raise ValidationError("Capacidad o acción de gestión no admitida.")
    manager = require_commercial_permission(actor, MANAGE)
    with transaction.atomic():
        users = {u.pk: u for u in get_user_model().objects.select_for_update().filter(
            pk__in={manager.pk, user_id}).order_by("pk")}
        require_commercial_permission(users.get(manager.pk), MANAGE)
        target = require_commercial_permission(users.get(user_id))
        if target.is_superuser or target.groups.filter(permissions__codename=capability,
                permissions__content_type__app_label="quotes",
                permissions__content_type__model="quoterevision").exists():
            raise ValidationError("El permiso está heredado de un rol; no se modifica desde este gestor.")
        permission = Permission.objects.get(content_type__app_label="quotes",
            content_type__model="quoterevision", codename=capability)
        if enabled:
            target.user_permissions.add(permission)
        else:
            target.user_permissions.remove(permission)
        return target
