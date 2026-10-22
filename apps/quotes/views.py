"""Vistas de cotizaciones: descarga de PDF oficial, envío por correo y comparador M7.

Emisiones: ambas leen el mismo original archivado. El único renderer está en pdf.
El comparador (async) consulta proveedores reales, compara en MXN y no escribe en BD.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

import httpx
from asgiref.sync import sync_to_async

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.catalog.matching import IdentificadoresConfiables, ResultadoMatch, aemparejar_con_fallback
from apps.catalog.models import Category
from apps.providers.adapters.syscom import consultar_tipo_cambio
from apps.providers.contracts import ProductQuery, ProviderOffer
from apps.providers.errors import ProviderError
from apps.providers.models import Provider
from apps.providers.service import search_all

from .comparison import NIVEL_SIN_COINCIDENCIA, TipoCambio, comparar_configurado, tipo_cambio_manual
from .models import DefaultCommercialCondition, Quote, QuoteRevision
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
        # Mensajes propios de M8, sin datos internos: explican qué recargar o preparar.
        message = exc.messages[0] if exc.messages else "La revisión requiere emisión o recarga."
        return JsonResponse({"error": message}, status=409)
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


def _document_guard(data):
    """P6: lectura exacta opcional; la UI siempre manda identidad y estado guardados."""
    if data.get("exact") not in (True, "1"):
        return {}
    if hasattr(data, "getlist") and any(len(data.getlist(k)) != 1 for k in data):
        raise ValidationError("Identidad documental ambigua.")
    version = _number(data.get("edit_version"))
    status = data.get("expected_status")
    fingerprint = data.get("state_fingerprint")
    if version is None or status not in ("DRAFT", "ISSUED"):
        raise ValidationError("Falta versión/estado del documento.")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        raise ValidationError("Huella de documento inválida.")
    return {"expected_version": version, "state_fingerprint": fingerprint, "expected_status": status}


def cotizador_comercial(request):
    """Página del cotizador; entrega las condiciones activas que se precargan (M2)."""
    condiciones = list(DefaultCommercialCondition.objects.filter(active=True).values_list("text", flat=True))
    # 5A: empresas configuradas para «Cotizar como» (solo aparece si hay más de una).
    from .api import empresas_publicas
    return render(request, "cotizador.html", {"condiciones_predeterminadas": condiciones, "empresas": empresas_publicas()})


@login_required
def descargar_pdf_cotizacion(request, folio):
    """GET /cotizaciones/<folio>/pdf/ → descarga el PDF oficial.

    ?revision=N permite recuperar una versión concreta. Emisiones: bytes originales.
    """
    if request.method not in {"GET", "HEAD"}:
        return JsonResponse({"error": "Método no permitido."}, status=405)
    try:
        if request.GET.get("exact") == "1" and request.GET.get("revision") is None:
            raise ValidationError("Se requiere revisión exacta.")
        context, pdf_bytes, filename = obtener_documento(folio, actor=request.user,
            number=_number(request.GET.get("revision")), **_document_guard(request.GET))
    except Exception as exc:
        return _document_error(exc)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Quote-Document"] = "original" if context.get("archived_only") else "draft-or-historical"
    return response


@login_required
def enviar_cotizacion_correo(request, folio):
    """POST /api/cotizaciones/<folio>/enviar-correo/ → envía la cotización.

    Body JSON: {email, revision, subject?, message?, attach_pdf=true, exact?,
                edit_version?, expected_status?, state_fingerprint?}.
    Solo v2 emitida autorizada; adjunta original, no emite ni regenera emisiones.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Método no permitido."}, status=405)

    def unique_pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("JSON ambiguo.")
            result[key] = value
        return result

    try:
        payload = json.loads(request.body or "{}", object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return JsonResponse({"success": False, "error": "JSON inválido."}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"success": False, "error": "JSON debe ser un objeto."}, status=400)
    if set(payload) - {"email", "subject", "message", "attach_pdf", "revision", "exact", "edit_version", "state_fingerprint", "expected_status"}:
        return JsonResponse({"success": False, "error": "Campos de correo no admitidos."}, status=400)
    if any(not isinstance(payload.get(key, ""), str) for key in ("email", "subject", "message")) or type(payload.get("attach_pdf", True)) is not bool:
        return JsonResponse({"success": False, "error": "Tipos de datos inválidos."}, status=400)
    if len(payload.get("email", "")) > 254 or len(payload.get("subject", "")) > 255 or len(payload.get("message", "")) > 6000:
        return JsonResponse({"success": False, "error": "Datos del correo demasiado largos."}, status=400)
    email = payload.get("email", "").strip()
    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({"success": False, "error": "Correo destinatario inválido."}, status=400)

    try:
        guard = _document_guard(payload)
        if payload.get("revision") is None:
            raise ValidationError("Se requiere revisión exacta.")
        if guard and payload.get("attach_pdf", True) is not True:
            raise ValidationError("El envío comercial exacto adjunta siempre su original.")
        quote, pdf_bytes, filename = obtener_documento(folio, actor=request.user,
            number=_number(payload.get("revision")), allow_draft=False, customer_only=True, **guard)
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
        if msg.send() != 1:
            raise RuntimeError("El backend de correo no confirmó aceptación.")
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
# M7: comparador de proveedores en modo degradado (docs/26 y docs/28). La vista es
# async: los proveedores se consultan en paralelo sin ocupar el hilo de las vistas
# síncronas. comparador.html (M7b) solo pinta el contexto.
# ==============================================================================

PLANTILLA_COMPARADOR = "comparador.html"
# Por usuario y ventana (antes por IP: en una oficina con una sola IP lo compartían todos los asesores).
# Con la LocMemCache por defecto el contador es por proceso (docs/26 §6).
LIMITE_COTIZACIONES = 20
VENTANA_SEGUNDOS = 60
# Declaración del consumidor de M5 (docs/21): SYSCOM manda el número de parte del
# fabricante en model y mpn, verificado con la API real (docs/16 D11). Un proveedor
# que no esté aquí no autoriza ninguna clave.
CONFIANZA_POR_PROVEEDOR = {
    "syscom": IdentificadoresConfiables(mpn_confirmado=True, modelo_confirmado=True),
}
_ENTERO_POSITIVO = re.compile(r"[0-9]{1,10}")


@dataclass(frozen=True, slots=True)
class OfertaPresentada:
    """Lo que recibe la plantilla por cada oferta: el ranking de M6 y la identidad de M5."""

    oferta: ProviderOffer
    precio_comparable: Decimal  # en MXN
    es_mejor_precio: bool  # dentro de su proveedor
    match: ResultadoMatch
    proveedor_nombre: str  # Provider.name; las plantillas no indexan un dict con una variable
    nivel: int = 0  # coincidencia del título con la búsqueda (M6)

    @property
    def sin_coincidencia(self):
        """El proveedor la relacionó con la búsqueda, pero su título no la nombra."""
        return self.nivel == NIVEL_SIN_COINCIDENCIA


class _EntradaInvalida(ValueError):
    """Error de los parámetros; su mensaje se muestra tal cual al usuario."""


# Tipo de cambio del comparador recordado en la sesión: {"valor": "18.50", "fecha": ISO, "origen": "manual"|"syscom"}.
CLAVE_TIPO_CAMBIO = "comparador_tipo_cambio"
# 14A: el de SYSCOM se guarda 30 min (1 min tras un fallo) para no consultarlo en cada visita.
SEGUNDOS_TIPO_CAMBIO_SYSCOM = 30 * 60
SEGUNDOS_FALLO_TIPO_CAMBIO_SYSCOM = 60
ETIQUETAS_TIPO_CAMBIO_SYSCOM = {
    "normal": "contado", "preferencial": "preferencial", "un_dia": "a 1 día", "una_semana": "a 1 semana",
    "dos_semanas": "a 2 semanas", "tres_semanas": "a 3 semanas", "un_mes": "a 1 mes",
}


@require_GET
@login_required
async def cotizar_productos(request):
    """GET /comparador/?q=&sku=&categoria=&tipo_cambio= → comparador.html.

    El último tipo de cambio válido se recuerda en la sesión (solo en el comparador; el cotizador
    tiene su propio contexto con permisos) y se usa mientras no se envíe otro. Enviarlo vacío lo
    olvida; cerrar sesión también.
    """
    # 22A: al volver al comparador sin parámetros (menú) se muestra la última búsqueda de la sesión.
    ultima = await request.session.aget(CLAVE_ULTIMA_BUSQUEDA)
    if not request.GET and ultima:
        parametros = {k: v for k, v in ultima["params"].items() if v}
        return HttpResponseRedirect(f"{request.path}?{urlencode({**parametros, 'ultima': '1'})}")
    recordado = await request.session.aget(CLAVE_TIPO_CAMBIO)
    usado = None
    accion = request.GET.get("usar_tipo_cambio")
    if (accion == "syscom" and not request.GET.get("tipo_cambio", "").strip()) or accion == "actualizar":
        # «Usar» (14A) y «Actualizar» (opción 1): el valor sale del servidor, no del formulario, para poder
        # marcarlo como de SYSCOM. «Usar» respeta lo escrito a mano; «Actualizar» lo pide explícitamente.
        usado, _ = await tipo_cambio_sugerido()
        if usado is not None:
            parametros = request.GET.copy()
            parametros["tipo_cambio"] = f"{usado:f}"
            request.GET = parametros
    elif "tipo_cambio" not in request.GET and recordado:
        parametros = request.GET.copy()
        parametros["tipo_cambio"] = recordado["valor"]
        request.GET = parametros
    status, contexto = await buscar(request)
    await _recordar_ofertas_vistas(request.session, contexto)
    await _recordar_busqueda(request, contexto, status, ultima)
    await _recordar_tipo_cambio(request, contexto, recordado, usado, confirmar=accion == "confirmar")
    if contexto["tipo_cambio"] is None:
        contexto["tipo_cambio_sugerido"], contexto["tipo_cambio_syscom_fallo"] = await tipo_cambio_sugerido()
        contexto["tipo_cambio_sugerido_etiqueta"] = ETIQUETAS_TIPO_CAMBIO_SYSCOM.get(settings.SYSCOM_TIPO_CAMBIO_CAMPO, "")
    elif contexto["tipo_cambio"].origen == "syscom" or contexto["tipo_cambio_otro_dia"]:
        # Opción 1: si el aplicado salió de SYSCOM y SYSCOM ya tiene otro, se avisa con «Actualizar» (nunca solo).
        # 21A: si es de otro día, el aviso ofrece «Usar el de hoy» (también para uno capturado a mano).
        actual, _ = await tipo_cambio_sugerido()
        if actual is not None and actual != contexto["tipo_cambio"].valor:
            clave = "tipo_cambio_syscom_hoy" if contexto["tipo_cambio_otro_dia"] else "tipo_cambio_syscom_nuevo"
            contexto[clave] = actual
    # Fuera del event loop: los context processors pueden leer la sesión (ORM síncrono).
    return await sync_to_async(render)(request, PLANTILLA_COMPARADOR, contexto, status=status)


# 22A: última búsqueda del comparador en la sesión: {"params": {q, sku, categoria}, "fecha": ISO}.
CLAVE_ULTIMA_BUSQUEDA = "comparador_ultima_busqueda"


# 24C: fechas relativas en pantalla («hace 3 días») y la exacta para el title. Mismas reglas que
# window.cconorFechas (static/js/api.js). El PDF del cliente no las usa.
MESES_CORTOS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def _plural(n, uno, varios):
    return f"{n} {uno if n == 1 else varios}"


def hace_texto(fecha):
    minutos = int((timezone.now() - fecha).total_seconds() // 60)
    if minutos < 1:
        return "hace un momento"
    if minutos < 60:
        return f"hace {minutos} min"
    dias = (timezone.localdate() - timezone.localtime(fecha).date()).days
    if dias <= 0:
        return f"hace {_plural(minutos // 60, 'hora', 'horas')}"
    if dias < 30:
        return f"hace {_plural(dias, 'día', 'días')}"
    if dias < 365:
        return f"hace {_plural(dias // 30, 'mes', 'meses')}"
    return f"hace {_plural(dias // 365, 'año', 'años')}"


def fecha_exacta(fecha):
    local = timezone.localtime(fecha)
    return f"{local.day} {MESES_CORTOS[local.month - 1]} {local.year}, {local:%H:%M}"


async def _recordar_busqueda(request, contexto, status, ultima):
    """Guarda la búsqueda válida (también con proveedores caídos); un formulario vacío o «Nueva búsqueda» la olvida."""
    params = {k: request.GET.get(k, "").strip() for k in ("q", "sku", "categoria")}
    if params["q"] or params["sku"]:
        if status not in (400, 429):
            # La misma búsqueda conserva su hora: «hace N min» cuenta desde que se buscó, no desde que se volvió.
            fecha = ultima["fecha"] if ultima and ultima["params"] == params else timezone.now().isoformat()
            await request.session.aset(CLAVE_ULTIMA_BUSQUEDA, {"params": params, "fecha": fecha})
            if request.GET.get("ultima") == "1":
                contexto["busqueda_restaurada"] = hace_texto(datetime.fromisoformat(fecha))
    elif ultima and ("q" in request.GET or "sku" in request.GET or "nueva" in request.GET):
        await request.session.apop(CLAVE_ULTIMA_BUSQUEDA)


async def _recordar_tipo_cambio(request, contexto, recordado, usado=None, confirmar=False):
    """Guarda el tipo de cambio aplicado y conserva su hora y origen de captura mientras no cambie.

    21A: «Sigue vigente» (confirmar) guarda cuándo se revisó; el aviso de otro día cuenta desde ahí.
    """
    texto = request.GET.get("tipo_cambio", "").strip()
    tipo = contexto["tipo_cambio"]
    if tipo is None and texto and not contexto["error"]:
        # Formulario en blanco: buscar() no lee parámetros, pero se muestra el recordado o el recién usado.
        try:
            tipo = tipo_cambio_manual(texto, fecha=timezone.now())
        except (TypeError, ValueError):
            tipo = None
    if tipo is not None:
        if usado is not None and tipo.valor == usado:
            tipo = TipoCambio(tipo.valor, "syscom", tipo.fecha)
        confirmado = None
        if recordado and recordado["valor"] == f"{tipo.valor:f}" and usado is None:
            tipo = TipoCambio(tipo.valor, recordado.get("origen", tipo.origen), datetime.fromisoformat(recordado["fecha"]))
            confirmado = recordado.get("confirmado")
            if confirmar:
                confirmado = timezone.now().isoformat()
                await request.session.aset(CLAVE_TIPO_CAMBIO, {**recordado, "confirmado": confirmado})
        else:
            await request.session.aset(CLAVE_TIPO_CAMBIO, {"valor": f"{tipo.valor:f}", "fecha": tipo.fecha.isoformat(),
                                                           "origen": tipo.origen})
        contexto["tipo_cambio"] = tipo
        contexto["tipo_cambio_confirmado"] = datetime.fromisoformat(confirmado) if confirmado else None
        contexto["tipo_cambio_hace"], contexto["tipo_cambio_exacta"] = hace_texto(tipo.fecha), fecha_exacta(tipo.fecha)
        if confirmado:
            contexto["tipo_cambio_confirmado_hace"] = hace_texto(contexto["tipo_cambio_confirmado"])
            contexto["tipo_cambio_confirmado_exacta"] = fecha_exacta(contexto["tipo_cambio_confirmado"])
        revisado = contexto["tipo_cambio_confirmado"] or tipo.fecha
        contexto["tipo_cambio_otro_dia"] = timezone.localdate(revisado) != timezone.localdate()
    elif not texto and recordado:
        await request.session.apop(CLAVE_TIPO_CAMBIO)  # vacío a propósito: se olvida


# 19A: ofertas que el comparador mostró; la bandeja solo acepta estas, con datos del servidor.
CLAVE_OFERTAS_VISTAS = "comparador_ofertas_vistas"
MAX_OFERTAS_VISTAS = 100


async def _recordar_ofertas_vistas(session, contexto):
    if not contexto["ofertas"]:
        return
    vistas = dict(await session.aget(CLAVE_OFERTAS_VISTAS, {}))
    for presentada in contexto["ofertas"]:
        oferta = presentada.oferta
        clave = f"{oferta.provider_code}:{oferta.external_id}"
        vistas.pop(clave, None)  # reinsertar al final: las más recientes quedan detrás
        vistas[clave] = {"provider_code": oferta.provider_code, "provider_name": presentada.proveedor_nombre,
                         "external_id": oferta.external_id, "name": oferta.name, "model": oferta.model or "",
                         "price": f"{oferta.price:f}", "currency": oferta.currency}
    for clave in list(vistas)[:-MAX_OFERTAS_VISTAS]:
        del vistas[clave]
    await session.aset(CLAVE_OFERTAS_VISTAS, vistas)


async def tipo_cambio_sugerido():
    """14A: (pesos por dólar de SYSCOM | None, falló). None sin SYSCOM activo; nunca se aplica solo."""
    campo = settings.SYSCOM_TIPO_CAMBIO_CAMPO
    clave = f"comparador:tipo_cambio_syscom:{campo}"
    guardado = await cache.aget(clave)
    if guardado is not None:
        return (Decimal(guardado), False) if guardado else (None, True)
    proveedor = await Provider.objects.filter(code="syscom", active=True).only("base_url", "timeout_ms").afirst()
    if proveedor is None:
        return None, False
    try:
        async with httpx.AsyncClient(timeout=min(proveedor.timeout_ms, 4000) / 1000) as client:
            valor = await consultar_tipo_cambio(proveedor.base_url, campo, client)
    except (httpx.HTTPError, ProviderError, ValueError) as exc:
        logger.warning("No se pudo consultar el tipo de cambio de SYSCOM: %s", type(exc).__name__)
        await cache.aset(clave, "", SEGUNDOS_FALLO_TIPO_CAMBIO_SYSCOM)
        return None, True
    await cache.aset(clave, f"{valor:f}", SEGUNDOS_TIPO_CAMBIO_SYSCOM)
    return valor, False


async def buscar(request):
    """Flujo de búsqueda: M2 search_all → M6 por proveedor → M5 sobre lo que se muestra.

    Devuelve (status, contexto) con las seis claves de docs/26 §4 en todas las respuestas.
    200 sin q ni sku (formulario en blanco) y aunque caigan proveedores (aviso en
    proveedores_caidos); 400 si la entrada no es válida; 429 al superar el límite;
    503 si no queda ningún proveedor útil.
    """
    parametros = request.GET
    contexto = {
        "ofertas": [],
        "proveedores_caidos": [],
        "proveedores_caidos_nombres": [],
        "tipo_cambio": None,
        "categorias": [c async for c in Category.objects.filter(is_active=True).order_by("name")],
        "error": None,
    }
    if not parametros.get("q", "").strip() and not parametros.get("sku", "").strip():
        return 200, contexto  # sin búsqueda: no consulta a nadie ni cuenta en el límite

    if await _limite_superado(request):
        contexto["error"] = "Demasiadas búsquedas seguidas. Espera un minuto y vuelve a intentarlo."
        return 429, contexto
    try:
        categoria_id, tipo_cambio = _leer_parametros(parametros, contexto["categorias"])
    except _EntradaInvalida as exc:
        contexto["error"] = str(exc)
        return 400, contexto
    contexto["tipo_cambio"] = tipo_cambio

    consulta = ProductQuery(term=parametros.get("q", ""), sku=parametros.get("sku"))
    try:
        resultados = await search_all(consulta)
    except Exception:  # search_all ya absorbe los ProviderError: esto es un fallo del proceso.
        logger.exception("Fallo inesperado consultando proveedores")
        contexto["error"] = "No fue posible consultar a los proveedores en este momento."
        return 503, contexto
    if not resultados:
        contexto["error"] = "No hay proveedores activos para consultar."
        return 503, contexto

    nombres = {p.code: p.name async for p in Provider.objects.only("code", "name")}
    caidos = [r.provider_code for r in resultados if not r.ok]
    disponibles = [r for r in resultados if r.ok]
    if tipo_cambio is None and any(o.currency == "USD" for r in disponibles for o in r.offers):
        _asignar_caidos(contexto, sorted(caidos), nombres)
        contexto["error"] = "Captura el tipo de cambio (pesos por dólar) para comparar los precios en USD."
        return 400, contexto

    comparadas = []
    for resultado in disponibles:
        # Por proveedor: si M6 rechaza los datos de uno, solo ese queda fuera.
        try:
            comparacion = comparar_configurado([resultado], tipo_cambio=tipo_cambio,
                                               termino=None if consulta.sku else consulta.term)
        except (ValueError, TypeError) as exc:
            logger.warning("Comparación descartada de %s: %s", resultado.provider_code, type(exc).__name__)
            caidos.append(resultado.provider_code)
            continue
        comparadas.extend(comparacion.por_proveedor[resultado.provider_code])
    _asignar_caidos(contexto, sorted(caidos), nombres)
    if len(caidos) == len(resultados):
        contexto["error"] = "Ningún proveedor pudo responder en este momento. Inténtalo de nuevo en unos minutos."
        return 503, contexto

    # M5 solo sobre lo que se muestra: no cambia el ranking, añade la identidad canónica.
    matches = await asyncio.gather(*(
        aemparejar_con_fallback(
            c.oferta,
            categoria_id=categoria_id,
            confianza=CONFIANZA_POR_PROVEEDOR.get(c.oferta.provider_code, IdentificadoresConfiables()),
        )
        for c in comparadas
    ))
    contexto["ofertas"] = sorted(
        (OfertaPresentada(c.oferta, c.precio_comparable, c.es_mejor_precio, m,
                          nombres.get(c.oferta.provider_code, c.oferta.provider_code), c.nivel)
         for c, m in zip(comparadas, matches)),
        # Primero lo que coincide con la búsqueda; dentro de cada nivel, de más barata a más cara.
        key=lambda o: (o.nivel, o.precio_comparable, o.oferta.provider_code, o.oferta.external_id),
    )
    return 200, contexto


def _leer_parametros(parametros, categorias):
    """Valida categoría y tipo de cambio antes de consultar a ningún proveedor."""
    categoria_id = None
    texto = parametros.get("categoria", "").strip()
    if texto:
        # Id interno de una categoría activa: las del <select> del formulario.
        if not _ENTERO_POSITIVO.fullmatch(texto) or int(texto) not in {c.pk for c in categorias}:
            raise _EntradaInvalida("La categoría seleccionada no es válida.")
        categoria_id = int(texto)
    tipo_cambio = None
    texto = parametros.get("tipo_cambio", "").strip()
    if texto:
        try:
            tipo_cambio = tipo_cambio_manual(texto, fecha=timezone.now())
        except (TypeError, ValueError):
            raise _EntradaInvalida(
                "El tipo de cambio debe ser un decimal positivo con punto (ej. 17.50)."
            ) from None
    return categoria_id, tipo_cambio


def clave_limite(usuario, ip=""):
    """Contador del límite de búsquedas: por usuario; sin sesión (no debería llegar aquí), por IP."""
    return f"cotizar:usuario:{usuario.pk}" if usuario.is_authenticated else f"cotizar:ip:{ip}"


async def _limite_superado(request):
    clave = clave_limite(await request.auser(), request.META.get("REMOTE_ADDR", ""))
    await cache.aadd(clave, 0, VENTANA_SEGUNDOS)
    try:
        cuenta = await cache.aincr(clave)
    except ValueError:  # la clave caducó entre aadd y aincr
        await cache.aset(clave, 1, VENTANA_SEGUNDOS)
        cuenta = 1
    return cuenta > LIMITE_COTIZACIONES


def _asignar_caidos(contexto, codigos, nombres):
    contexto["proveedores_caidos"] = codigos
    contexto["proveedores_caidos_nombres"] = [nombres.get(codigo, codigo) for codigo in codigos]
