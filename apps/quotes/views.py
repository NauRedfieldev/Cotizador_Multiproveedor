"""Vistas de cotizaciones: descarga de PDF oficial y envío por correo.

Emisiones: ambas leen el mismo original archivado. El único renderer está en pdf.
"""

import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string

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
