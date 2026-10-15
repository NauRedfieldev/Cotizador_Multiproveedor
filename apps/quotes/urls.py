"""URLs de la app quotes: comparador multi-proveedor, descarga de PDF y envío por correo."""

from django.urls import path

from . import api, views

urlpatterns = [
    # M7: comparador de proveedores. 'comparador' va primero porque resolve() da el
    # nombre de la primera ruta y el menú de base.html lo usa para marcarla activa;
    # 'cotizar_productos' es el action del formulario (reverse encuentra la segunda).
    path("comparador/", views.cotizar_productos, name="comparador"),
    path("comparador/", views.cotizar_productos, name="cotizar_productos"),
    # M7a: misma búsqueda en JSON para el cotizador (docs/28).
    path("api/v1/products/search/", api.buscar_productos, name="api_buscar_productos"),
    # M7a: cotizaciones sobre los servicios de M8 (docs/28 y docs/25 M8 §2).
    path("api/v1/quotes/", api.crear_cotizacion, name="api_crear_cotizacion"),
    path("api/v1/quotes/<str:folio>/", api.detalle_cotizacion, name="api_detalle_cotizacion"),
    path("api/v1/quotes/<str:folio>/revisiones/<int:numero>/", api.guardar_revision, name="api_guardar_revision"),
    path("api/v1/quotes/<str:folio>/revisiones/<int:numero>/emitir/", api.emitir, name="api_emitir_revision"),
    path("api/v1/quotes/<str:folio>/revisiones/<int:numero>/nueva/", api.nueva_revision, name="api_nueva_revision"),
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
