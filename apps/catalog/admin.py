from django.contrib import admin

from .models import SinonimoRed


@admin.register(SinonimoRed)
class SinonimoRedAdmin(admin.ModelAdmin):
    list_display = ["abreviatura", "expansion"]
    search_fields = ["abreviatura", "expansion"]
