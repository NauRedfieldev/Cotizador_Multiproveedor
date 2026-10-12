"""Vistas de cotizaciones: búsqueda multi-proveedor, descarga de PDF oficial y envío por correo.

La búsqueda (M7, docs/24) es async y funciona en modo degradado: un proveedor
caído deja un aviso, nunca un 500.

Las de PDF y correo reutilizan `pdf.generate_quote_pdf()` como ÚNICA función
generadora de bytes del PDF (no duplicar).
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.catalog.matching import IdentificadoresConfiables, ResultadoMatch, aemparejar_con_fallback
from apps.catalog.models import Category
from apps.providers.contracts import ProductQuery, ProviderOffer
from apps.providers.models import Provider
from apps.providers.service import search_all

from .comparison import comparar_configurado, tipo_cambio_manual
from .pdf import generate_quote_pdf, get_quote_context, safe_filename

FROM_EMAIL = "ventas@cconor.com"


def descargar_pdf_cotizacion(request, folio):
    """GET /cotizaciones/<folio>/pdf/ → descarga el PDF oficial.

    Funciona con cualquier folio: usa datos reales si la cotización existe
    en BD y datos demo (misma forma que Resumen) si aún no está guardada.
    """
    quote = get_quote_context(folio)
    pdf_bytes = generate_quote_pdf(quote)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{safe_filename(quote["folio"])}"'
    return response


def enviar_cotizacion_correo(request, folio):
    """POST /api/cotizaciones/<folio>/enviar-correo/ → envía la cotización.

    Body JSON: {email, subject, message, attach_pdf=true}. Adjunta el MISMO
    PDF de `generate_quote_pdf()` cuando attach_pdf es verdadero.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Método no permitido."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"success": False, "error": "JSON inválido."}, status=400)

    email = (payload.get("email") or "").strip()
    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({"success": False, "error": "Correo destinatario inválido."}, status=400)

    quote = get_quote_context(folio)
    subject = (payload.get("subject") or "").strip() or (
        f"Cotización {quote['folio']} - CCONOR Soluciones Tecnológicas"
    )
    message_text = (payload.get("message") or "").strip()
    plain_body = message_text or (
        f"Adjuntamos la cotización {quote['folio']} de CCONOR Soluciones Tecnológicas."
    )
    html_body = render_to_string(
        "pdf/email_cotizacion.html", {"quote": quote, "message": message_text or plain_body}
    )

    msg = EmailMultiAlternatives(
        subject=subject, body=plain_body, from_email=FROM_EMAIL, to=[email]
    )
    msg.attach_alternative(html_body, "text/html")
    if payload.get("attach_pdf", True):
        msg.attach(safe_filename(quote["folio"]), generate_quote_pdf(quote), "application/pdf")

    try:
        msg.send()
    except Exception as exc:  # Ej. SMTP caído: reportar como error JSON, no 500 HTML
        return JsonResponse({"success": False, "error": f"No se pudo enviar: {exc}"}, status=500)

    return JsonResponse(
        {"success": True, "message": f"Cotización {quote['folio']} enviada a {email}."}
    )


# --- M7: búsqueda multi-proveedor en modo degradado (docs/24) -----------------------------

logger = logging.getLogger("apps.quotes")

PLANTILLA_COMPARADOR = "comparador.html"
# Por IP y ventana. Con la LocMemCache por defecto el contador es por proceso (docs/24).
LIMITE_COTIZACIONES = 10
VENTANA_SEGUNDOS = 60
# Declaración del consumidor de M5 (docs/21): SYSCOM manda el número de parte del
# fabricante en model y mpn, verificado con la API real (docs/16 D11). Un proveedor
# que no esté aquí no autoriza ninguna clave.
CONFIANZA_POR_PROVEEDOR = {
    "syscom": IdentificadoresConfiables(mpn_confirmado=True, modelo_confirmado=True),
}
_ENTERO_POSITIVO = re.compile(r"[0-9]+")


@dataclass(frozen=True, slots=True)
class OfertaPresentada:
    """Lo que recibe la plantilla por cada oferta: el ranking de M6 y la identidad de M5."""

    oferta: ProviderOffer
    precio_comparable: Decimal  # en MXN
    es_mejor_precio: bool  # dentro de su proveedor
    match: ResultadoMatch
    proveedor_nombre: str  # Provider.name; las plantillas no indexan un dict con una variable


class _EntradaInvalida(ValueError):
    """Error de los parámetros; su mensaje se muestra tal cual al usuario."""


@require_GET
async def cotizar_productos(request):
    """GET /cotizaciones/buscar/?q=&sku=&categoria=&tipo_cambio= → comparador.html.

    M2 search_all → M6 comparar_configurado → M5 aemparejar_con_fallback.
    200 aunque caigan proveedores (aviso en proveedores_caidos); 503 si no queda
    ninguno; 400 si la entrada no es válida; 429 al superar el límite.
    """
    if await _limite_superado(request):
        return await _responder(
            request, 429, error="Demasiadas búsquedas seguidas. Espera un minuto y vuelve a intentarlo."
        )
    try:
        consulta, categoria_id, tipo_cambio = _leer_parametros(request.GET)
    except _EntradaInvalida as exc:
        return await _responder(request, 400, error=str(exc))

    resultados = await search_all(consulta)
    caidos = [r.provider_code for r in resultados if not r.ok]
    disponibles = [r for r in resultados if r.ok]
    if not disponibles:
        return await _sin_proveedores(request, caidos, tipo_cambio)
    if tipo_cambio is None and any(o.currency == "USD" for r in disponibles for o in r.offers):
        return await _responder(
            request, 400, proveedores_caidos=caidos,
            error="Captura el tipo de cambio (pesos por dólar) para comparar los precios en USD.",
        )

    comparadas = []
    for resultado in disponibles:
        # Por proveedor: si M6 rechaza los datos de uno, solo ese queda fuera.
        try:
            comparacion = comparar_configurado([resultado], tipo_cambio=tipo_cambio)
        except (ValueError, TypeError) as exc:
            logger.warning("Comparación descartada de %s: %s", resultado.provider_code, type(exc).__name__)
            caidos.append(resultado.provider_code)
            continue
        comparadas.extend(comparacion.por_proveedor[resultado.provider_code])
    caidos.sort()
    if len(caidos) == len(resultados):
        return await _sin_proveedores(request, caidos, tipo_cambio)

    # M5 solo sobre lo que se muestra: no cambia el ranking, añade la identidad canónica.
    matches = await asyncio.gather(*(
        aemparejar_con_fallback(
            c.oferta,
            categoria_id=categoria_id,
            confianza=CONFIANZA_POR_PROVEEDOR.get(c.oferta.provider_code, IdentificadoresConfiables()),
        )
        for c in comparadas
    ))
    nombres = await _nombres_de_proveedores()
    ofertas = sorted(
        (OfertaPresentada(c.oferta, c.precio_comparable, c.es_mejor_precio, m,
                          nombres.get(c.oferta.provider_code, c.oferta.provider_code))
         for c, m in zip(comparadas, matches)),
        key=lambda o: (o.precio_comparable, o.oferta.provider_code, o.oferta.external_id),
    )
    return await _responder(
        request, 200, ofertas=ofertas, proveedores_caidos=caidos, tipo_cambio=tipo_cambio,
        nombres=nombres,
    )


def _leer_parametros(parametros):
    """Valida la entrada antes de consultar a ningún proveedor."""
    try:
        consulta = ProductQuery(term=parametros.get("q", ""), sku=parametros.get("sku"))
    except ValueError:
        raise _EntradaInvalida("Escribe qué buscar o el modelo exacto.") from None
    categoria_id = None
    texto = parametros.get("categoria", "").strip()
    if texto:
        if not _ENTERO_POSITIVO.fullmatch(texto) or int(texto) == 0:
            raise _EntradaInvalida("La categoría no es válida.")
        categoria_id = int(texto)
    tipo_cambio = None
    texto = parametros.get("tipo_cambio", "").strip()
    if texto:
        try:
            tipo_cambio = tipo_cambio_manual(texto, fecha=timezone.now())
        except ValueError:
            raise _EntradaInvalida(
                "El tipo de cambio debe ser un número positivo con punto decimal, p. ej. 17.50."
            ) from None
    return consulta, categoria_id, tipo_cambio


async def _limite_superado(request):
    clave = f"cotizar:{request.META.get('REMOTE_ADDR', '')}"
    await cache.aadd(clave, 0, VENTANA_SEGUNDOS)
    try:
        cuenta = await cache.aincr(clave)
    except ValueError:  # la clave caducó entre aadd y aincr
        await cache.aset(clave, 1, VENTANA_SEGUNDOS)
        cuenta = 1
    return cuenta > LIMITE_COTIZACIONES


async def _sin_proveedores(request, caidos, tipo_cambio):
    return await _responder(
        request, 503, proveedores_caidos=caidos, tipo_cambio=tipo_cambio,
        error="Ningún proveedor está disponible en este momento. Inténtalo de nuevo en unos minutos.",
    )


async def _nombres_de_proveedores():
    return {p.code: p.name async for p in Provider.objects.only("code", "name")}


async def _responder(request, status, *, ofertas=(), proveedores_caidos=(), tipo_cambio=None,
                     error=None, nombres=None):
    """Toda respuesta usa comparador.html con el mismo contrato de contexto (docs/24 §4)."""
    if nombres is None:
        nombres = await _nombres_de_proveedores()
    contexto = {
        "ofertas": list(ofertas),
        "proveedores_caidos": list(proveedores_caidos),
        "proveedores_caidos_nombres": [nombres.get(code, code) for code in proveedores_caidos],
        "tipo_cambio": tipo_cambio,
        # Para el <select> del formulario, también en 400, 429 y 503.
        "categorias": [c async for c in Category.objects.filter(is_active=True).order_by("name")],
    }
    if error is not None:
        contexto["error"] = error
    # Fuera del event loop: los context processors pueden leer la sesión (ORM síncrono).
    return await sync_to_async(render)(request, PLANTILLA_COMPARADOR, contexto, status=status)
