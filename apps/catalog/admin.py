from django.contrib import admin

from .models import ProductoCanonico, SinonimoRed


@admin.register(SinonimoRed)
class SinonimoRedAdmin(admin.ModelAdmin):
    list_display = ["abreviatura", "expansion"]
    search_fields = ["abreviatura", "expansion"]


@admin.register(ProductoCanonico)
class ProductoCanonicoAdmin(admin.ModelAdmin):
    list_display = ["marca", "modelo", "mpn", "sku_fabricante", "categoria"]
    list_filter = ["categoria", "marca_norm"]
    search_fields = ["marca", "modelo", "mpn", "sku_fabricante", "descripcion"]
    readonly_fields = [
        "marca_norm", "modelo_norm", "mpn_norm", "sku_norm",
        "descripcion_norm", "created_at", "updated_at",
    ]
