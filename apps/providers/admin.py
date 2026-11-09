from django.contrib import admin

from .models import Provider, ProviderToken, RawProviderProduct


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "active", "timeout_ms", "updated_at")
    list_filter = ("active",)
    search_fields = ("code", "name")

    # base_url decide a dónde viajan credenciales de producción (docs/37, auditoría run-2 #5): solo el
    # superusuario la fija, al dar de alta o al cambiarla; Provider.clean() valida el host.
    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def get_readonly_fields(self, request, obj=None):
        campos = super().get_readonly_fields(request, obj)
        return campos if request.user.is_superuser else (*campos, "base_url")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and "base_url" in form.changed_data:
            # El token se emitió para el host anterior: no debe viajar al nuevo.
            ProviderToken.objects.filter(provider=obj).delete()


@admin.register(RawProviderProduct)
class RawProviderProductAdmin(admin.ModelAdmin):
    list_display = ("provider", "external_id", "last_seen_at", "fetch_count")
    list_filter = ("provider",)
    search_fields = ("external_id",)
    readonly_fields = (
        "provider", "external_id", "content_hash", "payload",
        "first_seen_at", "last_seen_at", "fetch_count",
    )

    # Histórico crudo: solo lectura (se puede ver, pero no crear, editar ni borrar).
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProviderToken)
class ProviderTokenAdmin(admin.ModelAdmin):
    # El access_token nunca se muestra en el admin.
    exclude = ("access_token",)
    list_display = ("provider", "obtained_at", "expires_at")
    readonly_fields = ("provider", "obtained_at", "expires_at")

    def has_add_permission(self, request):
        return False
