from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied

from .models import DefaultCommercialCondition, QuoteCommercialConfirmation, QuoteRevision, QuoteSnapshot
from .permissions import require_commercial_permission
from .usuarios import FormularioCambiarContrasenaAdmin, FormularioLoginAdmin, _cerrar_sesiones

# Cambiar contraseña en /admin/ guarda solo la contraseña y respeta una revocación simultánea.
admin.site.password_change_form = FormularioCambiarContrasenaAdmin
# admin/login/ comparte el límite de intentos fallidos por IP con login/.
admin.site.login_form = FormularioLoginAdmin


class SoloSuperusuario:
    """Usuarios y grupos en /admin/ solo para el superusuario (el Dueño), aunque alguien tenga is_staff y
    permisos de auth: quien edita usuarios ahí puede darse superusuario. Los roles se gestionan en
    «Usuarios y permisos»."""

    def _es_superusuario(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_module_permission(self, request):
        return self._es_superusuario(request)

    def has_view_permission(self, request, obj=None):
        return self._es_superusuario(request)

    def has_add_permission(self, request):
        return self._es_superusuario(request)

    def has_change_permission(self, request, obj=None):
        return self._es_superusuario(request)

    def has_delete_permission(self, request, obj=None):
        return self._es_superusuario(request)


class UsuarioAdmin(SoloSuperusuario, UserAdmin):
    """Desactivar a alguien en /admin/ cierra sus sesiones, igual que en «Usuarios y permisos»."""

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and "is_active" in form.changed_data and not obj.is_active:
            _cerrar_sesiones(obj)


class GrupoAdmin(SoloSuperusuario, GroupAdmin):
    pass


admin.site.unregister(get_user_model())
admin.site.register(get_user_model(), UsuarioAdmin)
admin.site.unregister(Group)
admin.site.register(Group, GrupoAdmin)


class SnapshotReadOnlyAdmin(admin.ModelAdmin):
    def has_view_permission(self, request, obj=None):
        if not super().has_view_permission(request, obj):
            return False
        try:
            require_commercial_permission(request.user)
        except PermissionDenied:
            return False
        return True

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(QuoteRevision)
class QuoteRevisionAdmin(SnapshotReadOnlyAdmin):
    list_display = ("quote", "number", "status", "issued_at")
    list_filter = ("status",)


@admin.register(QuoteSnapshot)
class QuoteSnapshotAdmin(SnapshotReadOnlyAdmin):
    list_display = ("revision", "position", "name", "source_currency", "source_unit_price")


@admin.register(QuoteCommercialConfirmation)
class QuoteCommercialConfirmationAdmin(SnapshotReadOnlyAdmin):
    list_display = ("revision", "edit_version", "warning_code", "confirmed_by", "confirmed_at")


@admin.register(DefaultCommercialCondition)
class DefaultCommercialConditionAdmin(admin.ModelAdmin):
    """M2: textos que se precargan en cada cotización nueva; no cambia cotizaciones guardadas."""

    list_display = ("order", "text", "active")
    list_display_links = ("text",)
    list_editable = ("order", "active")
    list_filter = ("active",)
