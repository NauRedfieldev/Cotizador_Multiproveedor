"""URLs de la app quotes: búsqueda multi-proveedor, descarga de PDF y envío por correo."""

from django.urls import path

from . import views

urlpatterns = [
    path(
        "cotizaciones/buscar/",
        views.cotizar_productos,
        name="cotizar_productos",
    ),
    path(
        "cotizaciones/<str:folio>/pdf/",
        views.descargar_pdf_cotizacion,
        name="descargar_pdf_cotizacion",
    ),
    path(
        "api/cotizaciones/<str:folio>/enviar-correo/",
        views.enviar_cotizacion_correo,
        name="enviar_cotizacion_correo",
    ),
]
