"""Vistas de cotizaciones: descarga de PDF oficial, envío por correo y comparador M7b.

Emisiones: ambas leen el mismo original archivado. El único renderer está en pdf.
El comparador consulta proveedores reales, compara en MXN y no escribe en BD.
"""

import json
import logging
from dataclasses import dataclass
from decimal import Decimal

from asgiref.sync import async_to_sync

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import DatabaseError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import timezone

from apps.catalog.constants import EstadoMatch
from apps.catalog.matching import ResultadoMatch, emparejar_con_fallback
from apps.catalog.models import Category, ProductoCanonico
from apps.providers.contracts import ProductQuery, ProviderOffer
from apps.providers.models import Provider
from apps.providers.service import search_all

from .comparison import comparar_configurado, tipo_cambio_manual
from .models import Quote, QuoteRevision
from .pdf import build_demo_quote, generate_quote_pdf, money, safe_filename
from .snapshots import ArchivedPDFError, RevisionConflict, obtener_documento

logger = logging.getLogger(__name__)

FROM_EMAIL = "ventas@cconor.com"


def _document_error(exc):
    if isinstance(exc, (Quote.DoesNotExist, QuoteRevision.DoesNotExist)):
        return JsonResponse({"error": "Cotización o revisión no encontrada."}, status=404)
    if isinstance(exc, PermissionDenied):
        return JsonResponse({"error": "No tienes acceso a esta cotización."}, status=403)
    if isinstance(exc, ArchivedPDFError):
        return JsonResponse({"error": "PDF original no disponible o dañado; requiere recuperación."}, status=503)
    if isinstance(exc, RevisionConflict):
        return JsonResponse({"error": "La revisión requiere emisión o recarga."}, status=409)
    if isinstance(exc, ValidationError):
        return JsonResponse({"error": "Datos de solicitud inválidos."}, status=400)
    logger.error("M8: fallo al obtener el documento; no se sustituye por demo.", exc_info=True)
    return JsonResponse({"error": "No se pudo obtener el documento."}, status=500)


def _number(value):
    if value is None:
        return None
    if isinstance(value, str) and len(value) <= 10 and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise ValidationError("Revisión inválida.")
    return value


@login_required
def descargar_pdf_cotizacion(request, folio):
    """GET /cotizaciones/<folio>/pdf/ → descarga el PDF oficial.

    ?revision=N permite recuperar una versión concreta. Emisiones: bytes originales.
    """
    if request.method not in {"GET", "HEAD"}:
        return JsonResponse({"error": "Método no permitido."}, status=405)
    try:
        _, pdf_bytes, filename = obtener_documento(folio, actor=request.user, number=_number(request.GET.get("revision")))
    except Exception as exc:
        return _document_error(exc)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def enviar_cotizacion_correo(request, folio):
    """POST /api/cotizaciones/<folio>/enviar-correo/ → envía la cotización.

    Body JSON: {email, subject, message, attach_pdf=true, revision?}.
    Adjunta el original archivado; no emite ni regenera versiones emitidas.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Método no permitido."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"success": False, "error": "JSON inválido."}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"success": False, "error": "JSON debe ser un objeto."}, status=400)
    if any(not isinstance(payload.get(key, ""), str) for key in ("email", "subject", "message")) or type(payload.get("attach_pdf", True)) is not bool:
        return JsonResponse({"success": False, "error": "Tipos de datos inválidos."}, status=400)
    email = payload.get("email", "").strip()
    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({"success": False, "error": "Correo destinatario inválido."}, status=400)

    try:
        quote, pdf_bytes, filename = obtener_documento(folio, actor=request.user,
                                                      number=_number(payload.get("revision")), allow_draft=False)
    except Exception as exc:
        return _document_error(exc)
    quote["total_fmt"] = money(quote["total"])
    quote["total_currency_label"] = "moneda no registrada" if quote.get("legacy_unknown_currency") else "MXN"
    subject = (payload.get("subject") or "").strip() or (
        f"Cotización {quote['folio']} - CCONOR Soluciones Tecnológicas"
    )
    message_text = (payload.get("message") or "").strip()
    plain_body = message_text or (
        f"Adjuntamos la cotización {quote['folio']} de CCONOR Soluciones Tecnológicas."
    )
    html_body = render_to_string(
        "pdf/email_cotizacion.html", {"quote": quote, "message": message_text or plain_body,
                                     "attach_pdf": payload.get("attach_pdf", True)}
    )

    msg = EmailMultiAlternatives(
        subject=subject, body=plain_body, from_email=FROM_EMAIL, to=[email]
    )
    msg.attach_alternative(html_body, "text/html")
    if payload.get("attach_pdf", True):
        msg.attach(filename, pdf_bytes, "application/pdf")

    try:
        msg.send()
    except Exception:
        logger.error("M8: no se pudo enviar el correo.", exc_info=True)
        return JsonResponse({"success": False, "error": "No se pudo enviar el correo."}, status=500)

    return JsonResponse(
        {"success": True, "message": f"Cotización {quote['folio']} enviada a {email}."}
    )


def demo_pdf_cotizacion(request, folio):
    """Demostración sintética explícita, deshabilitada fuera de DEBUG."""
    if not settings.DEBUG:
        return JsonResponse({"error": "No encontrado."}, status=404)
    if request.method != "GET":
        return JsonResponse({"error": "Método no permitido."}, status=405)
    data = generate_quote_pdf(build_demo_quote(folio))
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="DEMO_{safe_filename(folio)}"'
    response["Cache-Control"] = "no-store"
    return response


# ==============================================================================
# M7b: comparador de proveedores renderizado 100% por Django Templates.
# ==============================================================================


@dataclass(frozen=True, slots=True)
class OfertaPresentada:
    """Fila del contrato M7 que comparador.html recorre en el contexto."""

    oferta: ProviderOffer
    precio_comparable: Decimal
    es_mejor_precio: bool
    match: ResultadoMatch
    proveedor_nombre: str


# SIN_MATCH compartido: frozen y sin candidatos, seguro de reutilizar.
_SIN_MATCH = ResultadoMatch(None, EstadoMatch.SIN_MATCH, None, None, "SIN_MATCH")


def _match_de_oferta(oferta, categoria_pk, catalogo_disponible):
    """M5b + M5c por oferta; sin catálogo canónico migrado degrada a SIN_MATCH."""
    if not catalogo_disponible:
        return _SIN_MATCH
    return emparejar_con_fallback(oferta, categoria_id=categoria_pk)


def _contexto_comparador():
    """Las seis claves del contrato, también en error y en la página en blanco."""
    return {
        "ofertas": [],
        "proveedores_caidos": [],
        "proveedores_caidos_nombres": [],
        "tipo_cambio": None,
        "categorias": Category.objects.filter(is_active=True).order_by("name"),
        "error": None,
    }


def _responder(request, contexto, status=200):
    return render(request, "comparador.html", contexto, status=status)


def _asignar_caidos(contexto, codigos, nombres):
    codigos = list(codigos)
    contexto["proveedores_caidos"] = codigos
    contexto["proveedores_caidos_nombres"] = [nombres.get(codigo, codigo) for codigo in codigos]


def _status_de_comparacion(exc):
    """400 si falta el tipo de cambio o la moneda no es comparable; si no, 503."""
    texto = str(exc).lower()
    if "tipo de cambio" in texto or "moneda" in texto:
        return 400
    return 503


def cotizar_productos(request):
    """GET /comparador/ → busca en los proveedores activos y compara en MXN.

    Siempre entrega las seis claves del contexto. 400 si la consulta no es
    válida; 503 si no hay proveedores activos o caen todos. Sin escrituras,
    sin datos demo y sin CSRF (el formulario es GET).
    """
    q = request.GET.get("q", "").strip()
    sku = request.GET.get("sku", "").strip()
    categoria_txt = request.GET.get("categoria", "").strip()
    tipo_cambio_txt = request.GET.get("tipo_cambio", "").strip()
    contexto = _contexto_comparador()

    if not q and not sku:
        return _responder(request, contexto)

    try:
        consulta = ProductQuery(term=q, sku=sku or None)
    except ValueError as exc:
        contexto["error"] = f"La búsqueda no es válida: {exc}"
        return _responder(request, contexto, 400)

    categoria_pk = None
    if categoria_txt:
        if not categoria_txt.isdigit():
            contexto["error"] = "La categoría seleccionada no es válida."
            return _responder(request, contexto, 400)
        categoria_pk = int(categoria_txt)
        if (
            categoria_pk < 1
            or categoria_pk > 2147483647
            or not contexto["categorias"].filter(pk=categoria_pk).exists()
        ):
            contexto["error"] = "La categoría seleccionada no es válida."
            return _responder(request, contexto, 400)

    tipo_cambio = None
    if tipo_cambio_txt:
        try:
            tipo_cambio = tipo_cambio_manual(tipo_cambio_txt, fecha=timezone.now())
        except (TypeError, ValueError):
            contexto["error"] = "El tipo de cambio debe ser un decimal positivo con punto (ej. 17.50)."
            return _responder(request, contexto, 400)
    contexto["tipo_cambio"] = tipo_cambio

    try:
        # async_to_sync cierra la conexión del ORM async; asyncio.run la dejaba abierta.
        resultados = async_to_sync(search_all)(consulta)
    except Exception:  # search_all absorbe ProviderError; esto es fallo del proceso.
        logger.exception("Fallo inesperado consultando proveedores")
        contexto["error"] = "No fue posible consultar a los proveedores en este momento."
        return _responder(request, contexto, 503)

    if not resultados:
        contexto["error"] = "No hay proveedores activos para consultar."
        return _responder(request, contexto, 503)

    nombres = {p.code: p.name for p in Provider.objects.all()}
    if all(not resultado.ok for resultado in resultados):
        _asignar_caidos(contexto, [r.provider_code for r in resultados], nombres)
        contexto["error"] = "Ningún proveedor pudo responder en este momento."
        return _responder(request, contexto, 503)

    try:
        comparacion = comparar_configurado(resultados, tipo_cambio=tipo_cambio)
    except ValueError as exc:
        # M6 no arma ranking parcial: USD sin tipo de cambio u otra moneda inválida.
        _asignar_caidos(contexto, [r.provider_code for r in resultados if not r.ok], nombres)
        contexto["error"] = str(exc)
        return _responder(request, contexto, _status_de_comparacion(exc))

    _asignar_caidos(contexto, comparacion.errores, nombres)

    try:
        catalogo_disponible = ProductoCanonico.objects.exists()
    except DatabaseError:
        # catalog/0004 aún sin aplicar en esta BD (ProgrammingError en PG,
        # OperationalError en SQLite): matching degradado a SIN_MATCH.
        logger.warning("ProductoCanonico no disponible; matching deshabilitado.")
        catalogo_disponible = False

    filas = sorted(
        (fila for grupo in comparacion.por_proveedor.values() for fila in grupo),
        key=lambda fila: (fila.precio_comparable, fila.oferta.external_id),
    )
    contexto["ofertas"] = [
        OfertaPresentada(
            oferta=fila.oferta,
            precio_comparable=fila.precio_comparable,
            es_mejor_precio=fila.es_mejor_precio,
            match=_match_de_oferta(fila.oferta, categoria_pk, catalogo_disponible),
            proveedor_nombre=nombres.get(fila.oferta.provider_code, fila.oferta.provider_code),
        )
        for fila in filas
    ]
    return _responder(request, contexto)
