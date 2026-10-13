"""URLs de la app quotes: descarga de PDF y envío por correo."""

from django.urls import path

from . import views

urlpatterns = [
    # M7b: comparador de proveedores. 'cotizar_productos' es el action del
    # formulario; 'comparador' es el nombre que usa la navegación de base.html.
    path("comparador/", views.cotizar_productos, name="cotizar_productos"),
    path("comparador/", views.cotizar_productos, name="comparador"),
    path("demo/cotizaciones/<str:folio>/pdf/", views.demo_pdf_cotizacion, name="demo_pdf_cotizacion"),
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
