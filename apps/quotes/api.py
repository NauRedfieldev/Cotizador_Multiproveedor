"""API JSON de M7a (docs/28): búsqueda multi-proveedor y cotizaciones M8 para el cotizador.

La búsqueda reutiliza el flujo del comparador (views.buscar) y responde el contrato de
docs/27 §B2. El dinero viaja como texto y nunca pasa por float. Las ofertas devueltas se
guardan en la sesión: al guardar una cotización (M8) el precio, la moneda y el tipo de cambio
salen de ahí, no del navegador.
"""

import json
import logging
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from apps.providers.contracts import ProviderOffer

from .comparison import TipoCambio
from .models import Quote, QuoteRevision, rfc_validator
from .snapshots import (
    ArchivedPDFError, PartidaCapturada, RevisionConflict, acrear_revision, aemitir_revision,
    aguardar_borrador, authorize, crear_borrador,
)
from .views import buscar

logger = logging.getLogger(__name__)

# Ofertas vistas por el usuario, por "provider_code:external_id". Una búsqueda nueva pisa la
# entrada; se conservan las más recientes. La frescura del precio la decide M9 (docs/25 M8 §6).
CLAVE_OFERTAS = "m7a_ofertas"
MAX_OFERTAS_EN_SESION = 200


def sesion_requerida(vista):
    """Como login_required, pero una API responde 401 en JSON en lugar de redirigir al login."""

    @wraps(vista)
    async def envoltura(request, *args, **kwargs):
        if not (await request.auser()).is_authenticated:
            return JsonResponse({"success": False, "error": "Inicia sesión para continuar."}, status=401)
        return await vista(request, *args, **kwargs)

    return envoltura


@require_GET
@sesion_requerida
async def buscar_productos(request):
    """GET /api/v1/products/search/?q=&sku=&categoria=&tipo_cambio= → JSON.

    Mismos códigos que el comparador: 200 (también en modo degradado), 400, 429 y 503,
    todos con el mismo cuerpo y success=false en los errores.
    """
    if not request.GET.get("q", "").strip() and not request.GET.get("sku", "").strip():
        # El comparador pinta un formulario en blanco; una API sin consulta es un error.
        return JsonResponse(_cuerpo({"error": "Escribe qué buscar o el modelo exacto."}), status=400)
    status, contexto = await buscar(request)
    if contexto["ofertas"]:
        await _guardar_ofertas(request.session, contexto)
    return JsonResponse(_cuerpo(contexto), status=status)


def clave_oferta(provider_code, external_id):
    return f"{provider_code}:{external_id}"


def _texto(valor):
    """Decimal → texto sin notación científica."""
    return f"{valor:f}"


def _tipo_cambio(tipo):
    if tipo is None:
        return None
    return {"valor": _texto(tipo.valor), "origen": tipo.origen, "fecha": tipo.fecha.isoformat()}


def _cuerpo(contexto):
    error = contexto.get("error")
    data = [_oferta(o) for o in contexto.get("ofertas", [])]
    return {
        "success": error is None,
        "error": error,
        "count": len(data),
        "data": data,
        "proveedores_caidos": list(contexto.get("proveedores_caidos", [])),
        "proveedores_caidos_nombres": list(contexto.get("proveedores_caidos_nombres", [])),
        "tipo_cambio": _tipo_cambio(contexto.get("tipo_cambio")),
    }


def _oferta(presentada):
    oferta, match = presentada.oferta, presentada.match
    canonico = match.canonico
    return {
        "provider": oferta.provider_code,
        "provider_label": presentada.proveedor_nombre,
        "external_id": oferta.external_id,
        "name": oferta.name,
        "brand": oferta.brand,
        "model": oferta.model,
        "mpn": oferta.mpn,
        "price_original": _texto(oferta.price),
        "currency": oferta.currency,
        "precio_mxn": _texto(presentada.precio_comparable),
        "es_mejor_precio": presentada.es_mejor_precio,
        "stock": oferta.stock,
        "match": {
            "estado": match.estado.value,
            "estado_label": match.estado.label,
            # Solo una identidad confirmada: una sugerencia REVISION_MANUAL no lo es.
            "canonico": None if canonico is None else {
                "id": canonico.pk,
                "descripcion": canonico.descripcion,
                "marca": canonico.marca,
                "modelo": canonico.modelo,
            },
            "score": match.score,
            "motivo": match.motivo,
        },
    }


async def _guardar_ofertas(session, contexto):
    """Guarda en la sesión lo necesario para una PartidaCapturada de M8, con su tipo de cambio."""
    guardadas = dict(await session.aget(CLAVE_OFERTAS, {}))
    tipo_cambio = _tipo_cambio(contexto["tipo_cambio"])
    capturada = timezone.now().isoformat()
    for presentada in contexto["ofertas"]:
        oferta = presentada.oferta
        clave = clave_oferta(oferta.provider_code, oferta.external_id)
        guardadas.pop(clave, None)  # reinsertar al final: las más recientes quedan detrás
        guardadas[clave] = {
            "provider_code": oferta.provider_code,
            "provider_name": presentada.proveedor_nombre,
            "external_id": oferta.external_id,
            "name": oferta.name,
            "brand": oferta.brand or "",
            "model": oferta.model or "",
            "mpn": oferta.mpn or "",
            "price": _texto(oferta.price),
            "currency": oferta.currency,
            "stock": oferta.stock,
            "es_mejor_precio": presentada.es_mejor_precio,
            "tipo_cambio": tipo_cambio,
            "captured_at": capturada,
        }
    for clave in list(guardadas)[:-MAX_OFERTAS_EN_SESION]:
        del guardadas[clave]
    await session.aset(CLAVE_OFERTAS, guardadas)


# ==============================================================================
# Cotizaciones (M8, docs/25 M8 §2–§5). Las vistas solo validan la frontera HTTP y llaman a
# los servicios: no escriben revisiones ni partidas y no envuelven la emisión en atomic.
# Margen y precio de venta los decide M8 con CCONOR; mientras tanto la emisión está bloqueada
# (COTIZACIONES_EMISION_HABILITADA). La empresa sale de settings.CCONOR_EMPRESA (docs/28 §12).
# ==============================================================================

_CAMPOS_CLIENTE = {"client_name", "client_rfc", "client_email", "valid_days", "conditions", "items"}
# notes es una nota interna de la Quote: solo en el alta y nunca va al PDF.
_CAMPOS_ALTA = _CAMPOS_CLIENTE | {"request_id", "notes"}
_CAMPOS_EDICION = _CAMPOS_CLIENTE | {"edit_version"}
_CAMPOS_PARTIDA = {"provider", "external_id", "quantity", "description"}
_INTENTOS_FOLIO = 3


class _EntradaInvalida(ValueError):
    """400: el mensaje se muestra tal cual."""


class _OfertaNoDisponible(ValueError):
    """409: el precio no está en la sesión (o mezcla tipos de cambio); hay que volver a buscar."""


class _EmpresaSinConfigurar(Exception):
    """503: faltan los datos de la empresa en el servidor; nunca se sustituyen por ejemplos."""


class _EmisionDeshabilitada(Exception):
    """403: no hay política de precios de venta; emitir daría un documento a costo."""


def _api(metodo):
    """Método único, sesión obligatoria y errores de los servicios traducidos a JSON."""

    def decorador(vista):
        @require_http_methods([metodo])
        @sesion_requerida
        @wraps(vista)
        async def envoltura(request, *args, **kwargs):
            try:
                return await vista(request, *args, **kwargs)
            except Exception as exc:
                return _respuesta_de_error(exc)

        return envoltura

    return decorador


def _respuesta_de_error(exc):
    # RevisionConflict hereda de ValidationError: va antes.
    if isinstance(exc, (RevisionConflict, _OfertaNoDisponible)):
        status, mensaje = 409, "; ".join(getattr(exc, "messages", None) or [str(exc)])
    elif isinstance(exc, _EntradaInvalida):
        status, mensaje = 400, str(exc)
    elif isinstance(exc, ValidationError):
        status, mensaje = 400, "; ".join(exc.messages)
    elif isinstance(exc, TypeError):
        status, mensaje = 400, "Datos de solicitud inválidos."
    elif isinstance(exc, PermissionDenied):
        status, mensaje = 403, "No tienes acceso a esta cotización."
    elif isinstance(exc, ObjectDoesNotExist):
        status, mensaje = 404, "Cotización o revisión no encontrada."
    elif isinstance(exc, ArchivedPDFError):
        status, mensaje = 503, "PDF original no disponible o dañado; requiere recuperación."
    elif isinstance(exc, _EmpresaSinConfigurar):
        status, mensaje = 503, "Datos de la empresa sin configurar en el servidor; avisa al administrador."
    elif isinstance(exc, _EmisionDeshabilitada):
        status, mensaje = 403, (
            "La emisión comercial está deshabilitada hasta definir la política de precios (M8/CCONOR)."
        )
    else:
        logger.error("M7a: fallo inesperado en la API de cotizaciones.", exc_info=exc)
        status, mensaje = 500, "No se pudo completar la operación."
    return JsonResponse({"success": False, "error": mensaje}, status=status)


@_api("POST")
async def crear_cotizacion(request):
    """POST /api/v1/quotes/ → Quote + primer borrador (M8 crear_borrador). Idempotente por request_id."""
    empresa = _empresa()
    datos = _leer_json(request, _CAMPOS_ALTA)
    request_id = _uuid(datos.get("request_id"))
    cliente = _cliente(datos)
    partidas, tipo_cambio = _partidas(datos.get("items"), await request.session.aget(CLAVE_OFERTAS, {}))
    revision = await sync_to_async(_crear_cotizacion, thread_sensitive=True)(
        await request.auser(), empresa, cliente, partidas, tipo_cambio, request_id,
    )
    return JsonResponse({"success": True, "data": revision}, status=201)


@_api("GET")
async def detalle_cotizacion(request, folio):
    """GET /api/v1/quotes/<folio>/ → estado para recargar tras un 409 (solo propietario o staff)."""
    datos = await sync_to_async(_detalle, thread_sensitive=True)(folio, await request.auser())
    return JsonResponse({"success": True, "data": datos})


@_api("PUT")
async def guardar_revision(request, folio, numero):
    """PUT /api/v1/quotes/<folio>/revisiones/<n>/ → reemplazo completo del borrador (M8 guardar_borrador)."""
    datos = _leer_json(request, _CAMPOS_EDICION)
    version = _entero(datos.get("edit_version"), "edit_version")
    cliente = _cliente(datos)
    partidas, tipo_cambio = _partidas(datos.get("items"), await request.session.aget(CLAVE_OFERTAS, {}))
    actual = await QuoteRevision.objects.aget(quote__folio=folio, number=numero)
    revision = await aguardar_borrador(
        actual.pk, partidas, actor=await request.auser(), expected_version=version,
        company=actual.header["company"], conditions=cliente["conditions"],
        # El cliente se reemplaza con lo capturado; contacto, teléfono y proyecto no se editan aquí.
        client={**actual.header["client"], "business_name": cliente["client_name"],
                "rfc": cliente["client_rfc"], "email": cliente["client_email"]},
        tipo_cambio=tipo_cambio, tax_rate=actual.tax_rate, issue_date=actual.issue_date,
        valid_days=cliente["valid_days"],
    )
    return JsonResponse({"success": True, "data": _resumen(revision, folio)})


@_api("POST")
async def emitir(request, folio, numero):
    """POST …/revisiones/<n>/emitir/ → emisión con PDF original (M8 emitir_revision)."""
    if not settings.COTIZACIONES_EMISION_HABILITADA:
        raise _EmisionDeshabilitada()
    datos = _leer_json(request, {"edit_version"})
    actual = await QuoteRevision.objects.aget(quote__folio=folio, number=numero)
    revision = await aemitir_revision(
        actual.pk, actor=await request.auser(), expected_version=_entero(datos.get("edit_version"), "edit_version"),
    )
    return JsonResponse({"success": True, "data": _resumen(revision, folio)})


@_api("POST")
async def nueva_revision(request, folio, numero):
    """POST …/revisiones/<n>/nueva/ → borrador nuevo desde una emisión (M8 crear_revision)."""
    datos = _leer_json(request, {"request_id"})
    actual = await QuoteRevision.objects.aget(quote__folio=folio, number=numero)
    revision = await acrear_revision(
        actual.pk, actor=await request.auser(), request_id=_uuid(datos.get("request_id")),
    )
    return JsonResponse({"success": True, "data": _resumen(revision, folio)}, status=201)


# --- Frontera: el JSON se valida aquí; M8 vuelve a validar precisión y rangos -------------

def _leer_json(request, permitidos):
    try:
        datos = json.loads(request.body, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError):
        raise _EntradaInvalida("JSON inválido.") from None
    if not isinstance(datos, dict):
        raise _EntradaInvalida("El cuerpo debe ser un objeto JSON.")
    desconocidos = set(datos) - permitidos
    if desconocidos:
        raise _EntradaInvalida(f"Campos no admitidos: {', '.join(sorted(desconocidos))}.")
    return datos


def _texto_requerido(valor, campo, limite):
    if not isinstance(valor, str) or len(valor) > limite:
        raise _EntradaInvalida(f"{campo} debe ser texto de hasta {limite} caracteres.")
    return valor.strip()


def _entero(valor, campo):
    if type(valor) is not int:
        raise _EntradaInvalida(f"{campo} debe ser un entero.")
    return valor


def _uuid(valor):
    try:
        return UUID(valor)
    except (TypeError, ValueError, AttributeError):
        raise _EntradaInvalida("request_id debe ser un UUID; reutilízalo en los reintentos.") from None


def _cliente(datos):
    nombre = _texto_requerido(datos.get("client_name"), "client_name", 255)
    if not nombre:
        raise _EntradaInvalida("La razón social del cliente es obligatoria.")
    rfc = _texto_requerido(datos.get("client_rfc", ""), "client_rfc", 13)
    if rfc:
        rfc_validator(rfc)
    correo = _texto_requerido(datos.get("client_email", ""), "client_email", 254)
    if correo:
        validate_email(correo)
    condiciones = datos.get("conditions")
    # Obligatoria aunque vaya vacía: M8 no añade condiciones por defecto y aquí tampoco.
    if not isinstance(condiciones, list) or not all(isinstance(c, str) for c in condiciones):
        raise _EntradaInvalida("conditions debe ser una lista de textos para el cliente (puede ir vacía).")
    return {
        "client_name": nombre, "client_rfc": rfc, "client_email": correo,
        "valid_days": _entero(datos.get("valid_days", 15), "valid_days"),
        "notes": _texto_requerido(datos.get("notes", ""), "notes", 6000),
        "conditions": condiciones,
    }


def _empresa():
    """Datos de la empresa validados por CCONOR, del servidor; sin nombre no se crea nada."""
    empresa = dict(settings.CCONOR_EMPRESA)
    if not empresa.get("name", "").strip():
        raise _EmpresaSinConfigurar()
    return empresa


def _partidas(items, ofertas):
    """Partidas M8 con precio, moneda y tipo de cambio de la sesión; del navegador solo la cantidad."""
    if not isinstance(items, list) or not items:
        raise _EntradaInvalida("La cotización necesita al menos una partida.")
    partidas, tipos = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) - _CAMPOS_PARTIDA:
            raise _EntradaInvalida("Cada partida admite provider, external_id, quantity y description.")
        provider = _texto_requerido(item.get("provider"), "provider", 80)
        external_id = _texto_requerido(item.get("external_id"), "external_id", 6000)
        guardada = ofertas.get(clave_oferta(provider, external_id))
        if guardada is None:
            raise _OfertaNoDisponible(
                f"Vuelve a buscar el producto {external_id} de {provider}: su precio ya no está disponible."
            )
        cantidad = item.get("quantity")
        if not isinstance(cantidad, str):
            raise _EntradaInvalida("quantity debe ser texto, p. ej. \"2\" o \"1.5\".")
        try:
            cantidad = Decimal(cantidad)
        except InvalidOperation:
            raise _EntradaInvalida("quantity no es un número válido.") from None
        oferta = ProviderOffer(
            provider_code=guardada["provider_code"], external_id=guardada["external_id"],
            name=guardada["name"], price=Decimal(guardada["price"]), currency=guardada["currency"],
            brand=guardada["brand"], model=guardada["model"], mpn=guardada["mpn"], stock=guardada["stock"],
        )
        partida = PartidaCapturada.desde_oferta(oferta, cantidad, nombre_proveedor=guardada["provider_name"])
        partidas.append(replace(
            partida, es_mejor_precio=guardada["es_mejor_precio"],
            description=_texto_requerido(item.get("description", ""), "description", 6000),
        ))
        if guardada["currency"] == "USD":
            tipos.add(tuple(sorted(guardada["tipo_cambio"].items())))
    if len(tipos) > 1:
        raise _OfertaNoDisponible(
            "Las partidas en USD vienen de búsquedas con distinto tipo de cambio: vuelve a buscarlas con uno solo."
        )
    tipo_cambio = None
    if tipos:
        tipo = dict(tipos.pop())
        tipo_cambio = TipoCambio(Decimal(tipo["valor"]), tipo["origen"], datetime.fromisoformat(tipo["fecha"]))
    return partidas, tipo_cambio


# --- Operaciones síncronas (fuera del event loop, thread_sensitive) ----------------------

def _crear_cotizacion(usuario, empresa, cliente, partidas, tipo_cambio, request_id):
    # Dos altas simultáneas pueden calcular el mismo folio (Quote._generate_folio): se reintenta.
    for _ in range(_INTENTOS_FOLIO - 1):
        try:
            return _alta(usuario, empresa, cliente, partidas, tipo_cambio, request_id)
        except IntegrityError:
            pass
    return _alta(usuario, empresa, cliente, partidas, tipo_cambio, request_id)


def _alta(usuario, empresa, cliente, partidas, tipo_cambio, request_id):
    with transaction.atomic():
        # Serializa las altas del mismo usuario: un reintento simultáneo con el mismo
        # request_id encuentra la revisión de la primera en lugar de crear otra Quote.
        get_user_model().objects.select_for_update().filter(pk=usuario.pk).exists()
        previa = (QuoteRevision.objects.filter(request_id=request_id, quote__advisor=usuario)
                  .select_related("quote").first())
        if previa is not None:
            # notes no está en la huella de M8 (no va al PDF): se compara aquí.
            if previa.quote.notes != cliente["notes"]:
                raise RevisionConflict("request_id ya fue utilizado con datos diferentes.")
            quote_id = previa.quote_id
        else:
            quote = Quote(
                client_name=cliente["client_name"], client_rfc=cliente["client_rfc"],
                client_email=cliente["client_email"], advisor=usuario,
                valid_days=cliente["valid_days"], notes=cliente["notes"],
            )
            quote.full_clean(exclude=["folio"])
            quote.save()
            quote_id = quote.pk
        # Misma transacción: si M8 rechaza el borrador no queda una Quote huérfana.
        # Cliente y vigencia van explícitos (no los de la Quote) para que entren en la
        # huella: un reintento con otros datos es un conflicto, no se ignoran.
        revision = crear_borrador(
            quote_id, partidas, actor=usuario, company=empresa,
            conditions=cliente["conditions"], tipo_cambio=tipo_cambio, request_id=request_id,
            client={"business_name": cliente["client_name"], "rfc": cliente["client_rfc"],
                    "email": cliente["client_email"]},
            valid_days=cliente["valid_days"],
        )
        return _resumen(revision, revision.quote.folio)


def _detalle(folio, usuario):
    quote = Quote.objects.select_related("advisor").get(folio=folio)
    authorize(quote, usuario)
    revisiones = list(quote.revisions.order_by("number"))
    actual = revisiones[-1] if revisiones else None
    return {
        "folio": quote.folio,
        "status": quote.status,
        "revisiones": [_resumen(r, quote.folio) for r in revisiones],
        "actual": None if actual is None else {
            **_resumen(actual, quote.folio),
            "client": actual.header["client"],
            "conditions": actual.header["conditions"],
            "valid_days": actual.valid_days,
            "issue_date": actual.issue_date.isoformat(),
            "tax_rate": _texto(actual.tax_rate),
            "tipo_cambio": None if actual.exchange_rate is None else {
                "valor": _texto(actual.exchange_rate), "origen": actual.exchange_rate_source,
                "fecha": actual.exchange_rate_date.isoformat(),
            },
            "items": [
                {
                    "provider": s.provider_code, "provider_label": s.provider_name, "external_id": s.external_id,
                    "name": s.name, "description": s.description, "brand": s.brand, "model": s.model,
                    "mpn": s.mpn, "price_original": _texto(s.source_unit_price), "currency": s.source_currency,
                    "quantity": _texto(s.quantity), "stock": s.stock, "es_mejor_precio": s.es_mejor_precio,
                    "amount": None if s.amount is None else _texto(s.amount),
                }
                for s in actual.snapshots.all()
            ],
        },
    }


def _resumen(revision, folio):
    """Lo que el frontend guarda para la siguiente operación. Totales solo de una emisión (M8)."""
    return {
        "folio": folio,
        "revision": revision.number,
        "status": revision.status,
        "edit_version": revision.edit_version,
        "subtotal": None if revision.subtotal is None else _texto(revision.subtotal),
        "tax": None if revision.tax is None else _texto(revision.tax),
        "total": None if revision.total is None else _texto(revision.total),
    }
