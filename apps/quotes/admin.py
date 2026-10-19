from django.contrib import admin
from django.core.exceptions import PermissionDenied

from .models import DefaultCommercialCondition, QuoteCommercialConfirmation, QuoteRevision, QuoteSnapshot
from .permissions import require_commercial_permission


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
