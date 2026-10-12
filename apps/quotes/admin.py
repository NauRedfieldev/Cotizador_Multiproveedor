from django.contrib import admin

from .models import QuoteRevision, QuoteSnapshot


class SnapshotReadOnlyAdmin(admin.ModelAdmin):
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
