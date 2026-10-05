from django.contrib import admin

from .models import Provider, ProviderToken, RawProviderProduct


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "active", "timeout_ms", "updated_at")
    list_filter = ("active",)
    search_fields = ("code", "name")


@admin.register(RawProviderProduct)
class RawProviderProductAdmin(admin.ModelAdmin):
    list_display = ("provider", "external_id", "last_seen_at", "fetch_count")
    list_filter = ("provider",)
    search_fields = ("external_id",)
    readonly_fields = (
        "provider", "external_id", "content_hash", "payload",
        "first_seen_at", "last_seen_at", "fetch_count",
    )

    def has_add_permission(self, request):
        return False


@admin.register(ProviderToken)
class ProviderTokenAdmin(admin.ModelAdmin):
    # El access_token nunca se muestra en el admin.
    exclude = ("access_token",)
    list_display = ("provider", "obtained_at", "expires_at")
    readonly_fields = ("provider", "obtained_at", "expires_at")

    def has_add_permission(self, request):
        return False
