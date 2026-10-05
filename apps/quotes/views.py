"""Vistas de cotizaciones: descarga de PDF oficial y envío por correo.

Ambas reutilizan `pdf.generate_quote_pdf()` como ÚNICA función generadora
de bytes del PDF (no duplicar).
"""

import json

from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string

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
