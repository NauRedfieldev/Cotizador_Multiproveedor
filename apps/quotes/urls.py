"""URLs de la app quotes: comparador multi-proveedor, descarga de PDF y envío por correo."""

from django.urls import path

from . import views

urlpatterns = [
    # M7: comparador de proveedores. 'comparador' va primero porque resolve() da el
    # nombre de la primera ruta y el menú de base.html lo usa para marcarla activa;
    # 'cotizar_productos' es el action del formulario (reverse encuentra la segunda).
    path("comparador/", views.cotizar_productos, name="comparador"),
    path("comparador/", views.cotizar_productos, name="cotizar_productos"),
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
