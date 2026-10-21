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
    path("api/v1/quotes/preview/", api.preview_nueva, name="api_preview_nueva"),
    path("api/v1/quotes/fx-contexts/", api.crear_contexto_fx, name="api_contexto_fx"),
    path("api/v1/commercial/capabilities/", api.capacidades_comerciales, name="api_capacidades_comerciales"),
    # 15A/16A: tipo de cambio de la sesión (menú superior y cotizador).
    path("api/v1/fx/session/", api.tipo_cambio_sesion, name="api_tipo_cambio_sesion"),
    path("api/v1/commercial/permissions/<int:user_id>/", api.permisos_comerciales, name="api_permisos_comerciales"),
    # M7a: cotizaciones sobre los servicios de M8 (docs/28 y docs/25 M8 §2).
    path("api/v1/quotes/", api.crear_cotizacion, name="api_crear_cotizacion"),
    # 18B: clientes de las cotizaciones propias para «Elegir cliente».
    path("api/v1/clients/", api.clientes_frecuentes, name="api_clientes_frecuentes"),
    # 19A: bandeja del comparador (sesión) que el cotizador trae a una cotización.
    path("api/v1/tray/", api.bandeja, name="api_bandeja"),
    path("api/v1/quotes/<str:folio>/", api.detalle_cotizacion, name="api_detalle_cotizacion"),
    path("api/v1/quotes/<str:folio>/revisiones/<int:numero>/", api.guardar_revision, name="api_guardar_revision"),
    path("api/v1/quotes/<str:folio>/revisiones/<int:numero>/resumen/", api.resumen_documental, name="api_resumen_documental"),
    path("api/v1/quotes/<str:folio>/revisiones/<int:numero>/preview/", api.preview_edicion, name="api_preview_edicion"),
    path("api/v1/quotes/<str:folio>/revisiones/<int:numero>/confirmaciones/", api.confirmar_comercial, name="api_confirmar_comercial"),
    path("api/v1/quotes/<str:folio>/revisiones/<int:numero>/emitir/", api.emitir, name="api_emitir_revision"),
    path("api/v1/quotes/<str:folio>/revisiones/<int:numero>/nueva/", api.nueva_revision, name="api_nueva_revision"),
    path("api/v1/quotes/<str:folio>/revisiones/<int:numero>/nueva/preview/", api.preview_transicion, name="api_preview_transicion"),
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
