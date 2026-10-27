"""API JSON de M7a (docs/28): búsqueda multi-proveedor y cotizaciones M8 para el cotizador.

La búsqueda reutiliza el flujo del comparador (views.buscar) y responde el contrato de
docs/27 §B2. El dinero viaja como texto y nunca pasa por float. Ofertas v1 en sesión;
v2 usa capturas inmutables y recibos transaccionales. Originales API siempre del
servidor, nunca del navegador. DTO financiero interno separado de contexto cliente.
"""

import hashlib
import json
import re
import logging
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from uuid import UUID, uuid4

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.core.paginator import Paginator
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from apps.providers.contracts import ProviderOffer

from .comparison import TipoCambio
from .models import Quote, QuoteAPIRequest, QuoteRevision, QuoteSelectionContext, rfc_validator
from .pdf import COLORES_MARCA
from . import snapshots as commercial_services
from .permissions import ADJUSTMENTS, FX, MANAGE, VIEW_COSTS, require_commercial_permission, set_commercial_permission
from .snapshot_values import MAX_ITEMS, decimal_limit
from .taxes import TASA_IVA_INICIAL
from .snapshots import (
    ArchivedPDFError, PartidaCapturada, RevisionConflict, acrear_revision, aemitir_revision,
    authorize, crear_borrador,
)
from .views import (
    CLAVE_OFERTAS_VISTAS, CLAVE_TIPO_CAMBIO, ERROR_SIN_TIPO_CAMBIO, ERROR_SIN_TIPO_CAMBIO_SYSCOM, ETIQUETAS_TIPO_CAMBIO_SYSCOM,
    buscar, puede_cambiar_tipo_cambio, tipo_cambio_automatico, tipo_cambio_sugerido,
)

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
            return JsonResponse({"success": False, "error": "Inicia sesión para continuar.",
                                 "code": "UNAUTHENTICATED", "details": {}}, status=401)
        return await vista(request, *args, **kwargs)

    return envoltura


@require_GET
@sesion_requerida
async def buscar_productos(request):
    """GET /api/v1/products/search/?q=&sku=&categoria=&tipo_cambio= → JSON.

    Mismos códigos que el comparador: 200 (también en modo degradado), 400, 429 y 503,
    todos con el mismo cuerpo y success=false en los errores.
    """
    schema = request.GET.get("schema_version", "1")
    if schema not in ("1", "2"):
        return _respuesta_de_error(_EntradaInvalida("schema_version inválido."))
    fijo = fx_existente = None
    puede = True
    try:
        actor = await request.auser()
        await sync_to_async(require_commercial_permission, thread_sensitive=True)(actor)
        if schema == "2":
            # Lienzo fila 13: fx_context_id = el tipo de cambio de la cotización (ya autorizado al crearlo);
            # tipo_cambio = un valor a mano (permiso de tipo de cambio); sin ninguno, el de SYSCOM de hoy.
            if request.GET.get("fx_context_id"):
                if request.GET.get("tipo_cambio"):
                    raise _EntradaInvalida("Envía fx_context_id o tipo_cambio, no ambos.")
                fx_existente = await sync_to_async(_owned_context, thread_sensitive=True)(request.GET["fx_context_id"], actor, "FX")
                datos_fx = fx_existente.payload
                fijo = TipoCambio(Decimal(datos_fx["value"]), datos_fx["source"], datetime.fromisoformat(datos_fx["date"]))
            elif request.GET.get("tipo_cambio"):
                await sync_to_async(require_commercial_permission, thread_sensitive=True)(actor, FX)
            else:
                puede = await sync_to_async(puede_cambiar_tipo_cambio, thread_sensitive=True)(actor)
                valor, _ = await tipo_cambio_sugerido()
                if valor is not None:
                    fijo = TipoCambio(valor, "syscom", timezone.now())
    except Exception as exc:
        return _respuesta_de_error(exc)
    if not request.GET.get("q", "").strip() and not request.GET.get("sku", "").strip():
        # El comparador pinta un formulario en blanco; una API sin consulta es un error.
        return JsonResponse(_cuerpo({"error": "Escribe qué buscar o el modelo exacto."}), status=400)
    status, contexto = await buscar(request, tipo_cambio_fijo=fijo)
    if contexto.get("error") == ERROR_SIN_TIPO_CAMBIO and not puede:
        contexto["error"] = ERROR_SIN_TIPO_CAMBIO_SYSCOM
    if schema == "2":
        try:
            body = _cuerpo(contexto)
            if status == 200 and contexto["ofertas"] and not contexto.get("error"):
                capture, refs, fx_id = await sync_to_async(_capture_search, thread_sensitive=True)(actor, contexto, fx_existente)
                body.update(context_ref=str(capture.pk), fx_context_id=fx_id)
                for item, ref in zip(body["data"], refs, strict=True):
                    item.update(offer_ref=ref, context_ref=str(capture.pk), vat=capture.payload["offers"][ref]["vat"])
            return JsonResponse(body, status=status)
        except Exception as exc:
            return _respuesta_de_error(exc)
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
        "product_url": oferta.product_url,  # ficha pública en el proveedor; no se guarda en la captura
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
# P4 conecta el motor aprobado; la emisión HTTP sigue apagada hasta aceptación
# operativa. Empresa del servidor, no de datos demo ni del browser (docs/28 §13).
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


class _SinTipoCambioSyscom(Exception):
    """503: SYSCOM no respondió y no hay un valor suyo de hoy."""


SOLO_QUIEN_CAMBIA_TC = "Solo un Asesor o un Administrador puede cambiar el tipo de cambio."


class _EmisionDeshabilitada(Exception):
    """403: el despliegue no tiene autorización operativa de emisión HTTP."""


def _api(metodo):
    """Método único, sesión obligatoria y errores de los servicios traducidos a JSON."""

    def decorador(vista):
        @require_http_methods(metodo.split())
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
    elif isinstance(exc, _SinTipoCambioSyscom):
        status, mensaje = 503, "No hay tipo de cambio de SYSCOM en este momento; inténtalo en unos minutos."
    elif isinstance(exc, _EmisionDeshabilitada):
        status, mensaje = 403, (
            "La emisión comercial HTTP está deshabilitada; requiere validación y autorización operativa."
        )
    else:
        logger.error("M7a: fallo inesperado en la API de cotizaciones.", exc_info=exc)
        status, mensaje = 500, "No se pudo completar la operación."
    codes = {400: "INVALID_INPUT", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 503: "UNAVAILABLE", 500: "INTERNAL_ERROR"}
    details = exc.message_dict if isinstance(exc, ValidationError) and hasattr(exc, "message_dict") else {}
    return JsonResponse({"success": False, "error": mensaje, "code": codes[status], "details": details}, status=status)


@_api("GET POST")
async def crear_cotizacion(request):
    """GET listado propio; POST Quote + borrador. Alta idempotente por request_id."""
    if request.method == "GET":
        output = await sync_to_async(_listado_propio, thread_sensitive=True)(await request.auser(), request.GET)
        return JsonResponse({"success": True, **output}, headers={"Cache-Control": "private, no-store"})
    datos = _leer_json(request, _CAMPOS_ALTA | _COMMERCIAL_HEADER | {"schema_version", "company_key"})
    if "schema_version" in datos:
        _schema_v2(datos)
        output = await sync_to_async(_commercial_create, thread_sensitive=True)(await request.auser(), datos)
        return JsonResponse({"success": True, "data": output}, status=201)
    _fields(datos, _CAMPOS_ALTA)
    await sync_to_async(require_commercial_permission, thread_sensitive=True)(await request.auser())
    empresa = _empresa()
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


@_api("GET PUT")
async def guardar_revision(request, folio, numero):
    """PUT /api/v1/quotes/<folio>/revisiones/<n>/ → reemplazo completo del borrador (M8 guardar_borrador)."""
    if request.method == "GET":
        output = await sync_to_async(_exact_detail, thread_sensitive=True)(folio, numero, await request.auser())
        return JsonResponse({"success": True, "data": output})
    datos = _leer_json(request, _CAMPOS_EDICION | _COMMERCIAL_HEADER)
    output = await sync_to_async(_commercial_edit, thread_sensitive=True)(folio, numero, await request.auser(), datos)
    return JsonResponse({"success": True, "data": output})


def _legacy_edit_disabled():
    # La edición v1 anterior recapturaba originales desde sesión: no se utiliza
    # para cambios comerciales. Su servicio interno sigue disponible para histórico.
    raise RevisionConflict("Crea una revisión comercial v2 explícita antes de editar v1.")


@_api("POST")
async def emitir(request, folio, numero):
    """POST …/revisiones/<n>/emitir/ → emisión con PDF original (M8 emitir_revision)."""
    if not settings.COTIZACIONES_EMISION_HABILITADA:
        raise _EmisionDeshabilitada()
    datos = _leer_json(request, {"edit_version", "state_fingerprint"})
    actual = await sync_to_async(_emission_target, thread_sensitive=True)(folio, numero, await request.auser())
    if actual.schema_version != 2:
        raise RevisionConflict("La emisión HTTP requiere revisión comercial v2; no emitir a costo.")
    fingerprint = _hash_input(datos.get("state_fingerprint"))
    revision = await aemitir_revision(
        actual.pk, actor=await request.auser(), expected_version=_entero(datos.get("edit_version"), "edit_version"),
        state_fingerprint=fingerprint,
    )
    return JsonResponse({"success": True, "data": {**_resumen(revision, folio),
        "schema_version": revision.schema_version, "state_fingerprint": revision.state_fingerprint}})


@_api("POST")
async def nueva_revision(request, folio, numero):
    """POST …/revisiones/<n>/nueva/ → borrador nuevo desde una emisión (M8 crear_revision)."""
    datos = _leer_json(request, {"request_id", "schema_version", "items", "tax_rate", "discount_percent", "fx_context_id", "source_edit_version"})
    if datos.get("schema_version") == 2:
        output = await sync_to_async(_commercial_clone, thread_sensitive=True)(folio, numero, await request.auser(), datos)
        return JsonResponse({"success": True, "data": output}, status=201)
    _fields(datos, {"request_id"})
    actor = await sync_to_async(require_commercial_permission, thread_sensitive=True)(await request.auser())
    actual = await QuoteRevision.objects.aget(quote__folio=folio, number=numero)
    revision = await acrear_revision(
        actual.pk, actor=actor, request_id=_uuid(datos.get("request_id")),
    )
    return JsonResponse({"success": True, "data": _resumen(revision, folio)}, status=201)


# --- Frontera: el JSON se valida aquí; M8 vuelve a validar precisión y rangos -------------

def _leer_json(request, permitidos):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("Campo JSON duplicado.")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Constante JSON inválida.")

    try:
        datos = json.loads(request.body, parse_float=Decimal, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
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


EMPRESA_PRINCIPAL, EMPRESA_TORVA = "cconor", "torva"
_CAMPOS_EMPRESA = ("name", "rfc", "tagline", "phone", "email")


def _config_empresa(clave):
    """5A: configuración de una empresa (settings), o None si no está configurada.

    CCONOR: prefijo de folios nuevos en CCONOR_EMPRESA_FOLIO_PREFIX (vacío = COT, el de siempre).
    """
    if clave == EMPRESA_PRINCIPAL:
        config = dict(settings.CCONOR_EMPRESA)
        return {**config, "folio_prefix": config.get("folio_prefix", "").strip() or "COT", "logo": "img/logo.png"}
    if clave == EMPRESA_TORVA:
        config = dict(getattr(settings, "TORVA_EMPRESA", {}) or {})
        return config if config.get("name", "").strip() else None
    return None


def _empresa(clave=EMPRESA_PRINCIPAL):
    """Datos de la empresa validados por CCONOR, del servidor; sin nombre no se crea nada.

    5A: incluye su clave, que se guarda en la revisión y decide logo, filtros y prefijo de folio.
    """
    if not isinstance(clave, str) or clave not in (EMPRESA_PRINCIPAL, EMPRESA_TORVA):
        raise _EntradaInvalida("Empresa desconocida.")
    config = _config_empresa(clave)
    if config is None:
        raise _EntradaInvalida("Esa empresa no está configurada en el servidor.")
    if not config.get("name", "").strip():
        raise _EmpresaSinConfigurar()
    return {**{campo: config.get(campo, "") for campo in _CAMPOS_EMPRESA}, "key": clave}


def _prefijo_folio(clave):
    """Prefijo de los folios nuevos; cada empresa lleva su propio consecutivo, así que no pueden coincidir.

    Torva tampoco puede usar COT: es el de los folios anteriores de CCONOR.
    """
    prefijo = (_config_empresa(clave) or {}).get("folio_prefix", "").strip()
    otra = _config_empresa(EMPRESA_TORVA if clave == EMPRESA_PRINCIPAL else EMPRESA_PRINCIPAL)
    if not re.fullmatch(r"[A-Z]{2,6}", prefijo) or (otra and prefijo == otra.get("folio_prefix", "").strip()) \
            or (clave == EMPRESA_TORVA and prefijo == "COT"):
        raise _EmpresaSinConfigurar()
    return prefijo


# 5A (lienzo filas 9 y 11): color de cada marca, el mismo en pantalla, PDF y correo (definido en pdf.py).
COLORES_EMPRESA = COLORES_MARCA


def empresas_publicas():
    """Empresas configuradas para la interfaz: clave, nombre, RFC, iniciales, logo (URL o None), prefijo y colores."""
    from django.templatetags.static import static

    salida = []
    for clave in (EMPRESA_PRINCIPAL, EMPRESA_TORVA):
        config = _config_empresa(clave)
        if not config or not config.get("name", "").strip():
            continue
        palabras = [p for p in re.split(r"\s+", config["name"].strip()) if p[:1].isalnum()]
        iniciales = "".join(p[0] for p in palabras[:2]).upper() or config["name"].strip()[:2].upper()
        logo = config.get("logo", "").strip()
        # Lienzo fila 10: nombre corto automático (primera palabra, tal como está en el .env) para pestañas y etiquetas.
        salida.append({"key": clave, "name": config["name"].strip(), "short_name": config["name"].split()[0],
                       "rfc": config.get("rfc", "").strip(),
                       "initials": iniciales, "logo_url": static(logo) if logo else None,
                       "folio_prefix": config.get("folio_prefix", "").strip(), **COLORES_EMPRESA[clave]})
    return salida


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
            quote.folio_prefix = _prefijo_folio(empresa.get("key", EMPRESA_PRINCIPAL))   # 5A: prefijo vigente de CCONOR
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
    with transaction.atomic():
        return _detalle_locked(folio, usuario)


def _detalle_locked(folio, usuario):
    quote = Quote.objects.select_for_update(of=("self",)).select_related("advisor").get(folio=folio)
    usuario = require_commercial_permission(usuario)
    authorize(quote, usuario)
    revisiones = list(quote.revisions.order_by("number"))
    actual = revisiones[-1] if revisiones else None
    if actual is not None and actual.schema_version == 2:
        return {"folio": quote.folio, "status": quote.status,
            "revisiones": [_resumen(r, quote.folio) for r in revisiones],
            "actual": _exact_detail(folio, actual.number, usuario)}
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


def _listado_propio(actor, params):
    """Una fila por folio propio, sobre su última revisión, nunca QuoteItem.total."""
    actor = require_commercial_permission(actor)
    if set(params) - {"q", "status", "page", "empresa"} or any(len(params.getlist(k)) != 1 for k in params):
        raise _EntradaInvalida("Filtros de listado inválidos.")
    query = params.get("q", "").strip()
    status = params.get("status", "")
    page = params.get("page", "1")
    empresa = params.get("empresa", "")
    if len(query) > 200 or status not in ("", "DRAFT", "ISSUED") or not re.fullmatch(r"[1-9][0-9]{0,8}", page) \
            or empresa not in ("", EMPRESA_PRINCIPAL, EMPRESA_TORVA):
        raise _EntradaInvalida("Búsqueda, estado, empresa o página inválidos.")
    latest = QuoteRevision.objects.filter(quote_id=OuterRef("quote_id")).order_by("-number").values("pk")[:1]
    rows = QuoteRevision.objects.filter(quote__advisor_id=actor.pk).annotate(latest_id=Subquery(latest)).filter(pk=F("latest_id"))
    if query:
        rows = rows.filter(Q(quote__folio__icontains=query) | Q(header__client__business_name__icontains=query))
    if status:
        rows = rows.filter(status=status)
    # 5A: conteo por empresa con la búsqueda y el estado actuales (pestañas de «Mis cotizaciones»).
    # Las revisiones sin clave de empresa (anteriores a 5A) son de CCONOR.
    conteo = rows.aggregate(total=Count("pk"), torva=Count("pk", filter=Q(header__company__key=EMPRESA_TORVA)))
    company_counts = {"": conteo["total"], EMPRESA_PRINCIPAL: conteo["total"] - conteo["torva"], EMPRESA_TORVA: conteo["torva"]}
    if empresa == EMPRESA_TORVA:
        rows = rows.filter(header__company__key=EMPRESA_TORVA)
    elif empresa == EMPRESA_PRINCIPAL:
        rows = rows.filter(Q(header__company__key=EMPRESA_PRINCIPAL) | ~Q(header__company__has_key="key"))
    rows = rows.select_related("quote").annotate(item_count=Count("snapshots")).order_by("-created_at", "-pk")
    paginator = Paginator(rows, 20)
    if int(page) > paginator.num_pages:
        raise _EntradaInvalida("Página fuera de rango; vuelve al inicio del listado.")
    selected = paginator.page(int(page))
    return {"actor_id": actor.pk, "data": [{**_resumen(r, r.quote.folio), "schema_version": r.schema_version,
        "client_name": r.header["client"]["business_name"], "issue_date": r.issue_date.isoformat(),
        "items_count": r.item_count, "company_key": r.header.get("company", {}).get("key", EMPRESA_PRINCIPAL),
        "company_name": r.header.get("company", {}).get("name", "")} for r in selected],
        "pagination": {"page": selected.number, "pages": paginator.num_pages, "count": paginator.count},
        "companies": empresas_publicas(), "company_counts": company_counts}


# 19A: bandeja del comparador. Vive en la sesión; solo guarda referencias de lo que el comparador
# mostró. El cotizador vuelve a consultar al proveedor antes de agregar una partida.
CLAVE_BANDEJA = "comparador_bandeja"
MAX_BANDEJA = 30
MAX_PIEZAS_BANDEJA = 9999


def _clave_texto(valor):
    if not isinstance(valor, str) or not valor or len(valor) > 300:
        raise _EntradaInvalida("Producto de la bandeja inválido.")
    return valor


@_api("GET POST PATCH DELETE")
async def bandeja(request):
    """GET la lista; POST {"key"} agrega una pieza; PATCH {"key", "quantity"} fija la cantidad
    (0 la quita); DELETE {"keys": [...]} quita esos productos o, sin cuerpo, la vacía."""
    items = [dict(i) for i in await request.session.aget(CLAVE_BANDEJA, [])]
    if request.method == "POST":
        clave = _clave_texto(_leer_json(request, {"key"}).get("key"))
        actual = next((i for i in items if i["key"] == clave), None)
        if actual is not None:
            if actual["quantity"] >= MAX_PIEZAS_BANDEJA:
                raise _EntradaInvalida(f"Máximo {MAX_PIEZAS_BANDEJA} piezas por producto.")
            actual["quantity"] += 1
        else:
            vista = (await request.session.aget(CLAVE_OFERTAS_VISTAS, {})).get(clave)
            if vista is None:
                raise _OfertaNoDisponible("Ese producto ya no está en tus resultados; vuelve a buscarlo en el comparador.")
            if len(items) >= MAX_BANDEJA:
                raise _EntradaInvalida(f"La bandeja admite hasta {MAX_BANDEJA} productos; pasa algunos al cotizador.")
            items.append({"key": clave, **vista, "quantity": 1})
    elif request.method == "PATCH":
        data = _leer_json(request, {"key", "quantity"})
        clave, cantidad = _clave_texto(data.get("key")), data.get("quantity")
        if type(cantidad) is not int or not 0 <= cantidad <= MAX_PIEZAS_BANDEJA:
            raise _EntradaInvalida(f"La cantidad debe ser un entero entre 0 y {MAX_PIEZAS_BANDEJA}.")
        actual = next((i for i in items if i["key"] == clave), None)
        if actual is None:
            raise _OfertaNoDisponible("Ese producto ya no está en la bandeja.")
        actual["quantity"] = cantidad
        items = [i for i in items if i["quantity"] > 0]
    elif request.method == "DELETE":
        if request.body:
            claves = _leer_json(request, {"keys"}).get("keys")
            if not isinstance(claves, list) or len(claves) > MAX_BANDEJA:
                raise _EntradaInvalida("keys debe ser una lista de productos.")
            quitar = {_clave_texto(c) for c in claves}
            items = [i for i in items if i["key"] not in quitar]
        else:
            items = []
    if request.method != "GET":
        await request.session.aset(CLAVE_BANDEJA, items)
    return JsonResponse({"success": True, "data": items, "pieces": sum(i["quantity"] for i in items)})


MAX_CLIENTES = 50


def _clientes_frecuentes(actor, params):
    """18B: clientes de las cotizaciones propias, sin tabla nueva.

    Un cliente se reconoce por su RFC (o, sin RFC, por la razón social). Sus datos son los de la
    revisión más reciente; el proyecto no se devuelve porque cambia en cada cotización.
    """
    actor = require_commercial_permission(actor)
    if set(params) - {"q", "order"} or any(len(params.getlist(k)) != 1 for k in params):
        raise _EntradaInvalida("Filtros de clientes inválidos.")
    query = params.get("q", "").strip().casefold()
    order = params.get("order", "recent")
    if len(query) > 200 or order not in ("recent", "count", "az"):
        raise _EntradaInvalida("Búsqueda u orden inválidos.")
    latest = QuoteRevision.objects.filter(quote_id=OuterRef("quote_id")).order_by("-number").values("pk")[:1]
    rows = (QuoteRevision.objects.filter(quote__advisor_id=actor.pk).annotate(latest_id=Subquery(latest))
            .filter(pk=F("latest_id")).select_related("quote").order_by("-created_at", "-pk"))
    clientes = {}
    for revision in rows:
        datos = revision.header.get("client") or {}
        nombre = (datos.get("business_name") or "").strip()
        if not nombre:
            continue
        rfc = (datos.get("rfc") or "").strip().upper()
        clave = ("rfc", rfc) if rfc else ("nombre", " ".join(nombre.casefold().split()))
        if clave in clientes:  # ya está con sus datos más recientes; solo se cuenta
            clientes[clave]["quotes_count"] += 1
            continue
        clientes[clave] = {"business_name": nombre, "rfc": rfc, "email": datos.get("email") or "",
                           "contact": datos.get("contact") or "", "phone": datos.get("phone") or "",
                           "source_folio": revision.quote.folio, "last_date": revision.created_at.isoformat(),
                           "quotes_count": 1}
    lista = [c for c in clientes.values()
             if not query or any(query in c[k].casefold() for k in ("business_name", "rfc", "email", "contact"))]
    if order == "count":
        lista.sort(key=lambda c: -c["quotes_count"])  # sort estable: empates quedan por recientes
    elif order == "az":
        lista.sort(key=lambda c: c["business_name"].casefold())
    return {"data": lista[:MAX_CLIENTES], "count": len(lista)}


@_api("GET")
async def clientes_frecuentes(request):
    return JsonResponse({"success": True, **await sync_to_async(_clientes_frecuentes, thread_sensitive=True)(
        await request.auser(), request.GET)})


@_api("GET")
async def resumen_documental(request, folio, numero):
    def read(actor):
        with transaction.atomic():
            quote, revision = _get_commercial(folio, numero, actor)
            output = {**_resumen(revision, folio), "actor_id": actor.pk,
                "schema_version": revision.schema_version, "state_fingerprint": revision.state_fingerprint,
                "quote_status": quote.status, "client_name": revision.header["client"]["business_name"],
                "cliente": None, "warnings": [], "prices_captured_at": None,
                "emission_enabled": bool(settings.COTIZACIONES_EMISION_HABILITADA and revision.schema_version == 2),
                "revisiones": [{**_resumen(r, folio), "schema_version": r.schema_version}
                               for r in quote.revisions.order_by("number")]}
            if revision.schema_version == 2:
                if revision.status == "DRAFT":
                    preview = commercial_services.previsualizar_revision_comercial(revision.pk,
                        actor=actor, expected_version=revision.edit_version)
                    output["cliente"] = preview["cliente"]
                    output["warnings"] = _warning_states(preview["resultado"], revision, quote)
                    # 23C: el precio de proveedor más antiguo del borrador, para recordar revisarlo antes de emitir.
                    capturas = [s.captured_at for s in revision.snapshots.all() if s.source_kind == "API"]
                    output["prices_captured_at"] = min(capturas).isoformat() if capturas else None
                else:
                    output["cliente"] = commercial_services.contexto_revision(revision)
            return _wire(output)
    output = await sync_to_async(read, thread_sensitive=True)(await request.auser())
    return JsonResponse({"success": True, "data": output}, headers={"Cache-Control": "private, no-store"})


@_api("POST")
async def preview_transicion(request, folio, numero):
    data = _leer_json(request, {"schema_version", "items", "tax_rate", "discount_percent", "fx_context_id", "source_edit_version"})
    _schema_v2(data)
    def preview(actor):
        with transaction.atomic():
            quote, previous = _get_commercial(folio, numero, actor)
            if previous.schema_version != 1:
                raise RevisionConflict("Esta preparación explícita es solo para históricos v1.")
            if _entero(data.get("source_edit_version"), "source_edit_version") != previous.edit_version:
                raise RevisionConflict("El histórico cambió; vuelve a abrirlo antes de preparar la transición.")
            parts = _commercial_items(data.get("items"), actor)
            tipo, context_id = _fx_context(data.get("fx_context_id"), actor)
            tax = _decimal_text(data.get("tax_rate"), 5, 4, maximum=Decimal("1"))
            discount = _decimal_text(data.get("discount_percent"), 5, 2, maximum=Decimal("100"))
            result = commercial_services.previsualizar_comercial(quote.pk, parts, actor=actor,
                company=previous.header["company"], conditions=previous.header["conditions"], client=previous.header["client"],
                tipo_cambio=tipo, exchange_context_id=context_id, tax_rate=tax, discount_percent=discount,
                issue_date=timezone.localdate(), valid_days=previous.valid_days)
            result["warnings"] = _warning_states(result["resultado"], QuoteRevision(discount_percent=discount))
            return _wire(result)
    return JsonResponse({"success": True, "data": await sync_to_async(preview, thread_sensitive=True)(await request.auser())})


# P4: frontera comercial explícita. Las vistas no aceptan originales API del
# navegador, no consultan proveedores al guardar y no serializan el ORM completo.
_COMMERCIAL_HEADER = {"client_contact", "client_phone", "client_project", "issue_date",
                      "tax_rate", "discount_percent", "fx_context_id"}
_DECIMAL_TEXT = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


def _fields(data, allowed, required=()):
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise _EntradaInvalida("Campos no admitidos o requeridos ausentes.")
    return data


def _schema_v2(data):
    if type(data.get("schema_version")) is not int or data["schema_version"] != 2:
        raise _EntradaInvalida("schema_version debe ser 2.")


def _decimal_text(value, digits, places, *, maximum=None, minimum=Decimal("0")):
    if not isinstance(value, str) or len(value) > 80 or not _DECIMAL_TEXT.fullmatch(value):
        raise _EntradaInvalida("Se requiere texto decimal sin exponentes ni separadores.")
    return decimal_limit(Decimal(value), digits, places, minimum=minimum, maximum=maximum)


def _hash_input(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise _EntradaInvalida("state_fingerprint inválido.")
    return value


def _wire(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Decimal):
        return _texto(value)
    if isinstance(value, (UUID, date, datetime)):
        return str(value)
    if isinstance(value, dict):
        return {k: _wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(v) for v in value]
    return value


def _capture_search(actor, context, fx_existente=None):
    actor = require_commercial_permission(actor)
    offers = {}
    bases = getattr(settings, "CCONOR_PROVIDER_PRICE_BASES", {})
    for entry in context["ofertas"]:
        if len(offers) >= MAX_ITEMS:
            raise ValidationError("Demasiadas ofertas para una captura; acota la búsqueda.")
        offer = entry.oferta
        vat = bases.get(offer.provider_code, {"vat_basis": "UNKNOWN"})
        if not isinstance(vat, dict):
            raise ValidationError("Configuración de base IVA inválida.")
        if vat.get("vat_basis") == "UNKNOWN":
            _fields(vat, {"vat_basis"})
        else:
            _vat(vat)  # Configuración confiable, explícita, no raw ni inferencia por nombre.
        for text, limit in ((offer.name, 6000), (offer.external_id, 6000), (offer.provider_code, 80),
                            (entry.proveedor_nombre, 255), (offer.brand or "", 6000),
                            (offer.model or "", 6000), (offer.mpn or "", 6000)):
            commercial_services._text(text, limit=limit)
        offers[str(uuid4())] = {"name": offer.name, "price": _texto(decimal_limit(offer.price, 18, 6)),
            "currency": offer.currency, "provider_code": offer.provider_code,
            "provider_name": entry.proveedor_nombre, "external_id": offer.external_id,
            "brand": offer.brand or "", "model": offer.model or "", "mpn": offer.mpn or "",
            "stock": offer.stock, "es_mejor_precio": entry.es_mejor_precio, "vat": dict(vat)}
    with transaction.atomic():
        fx = fx_existente
        if fx is None and context["tipo_cambio"] is not None:
            tipo = context["tipo_cambio"]
            if tipo.origen == "manual":
                require_commercial_permission(actor, FX)
            fx = QuoteSelectionContext.objects.create(actor=actor, kind="FX", payload={
                "value": _texto(decimal_limit(tipo.valor, 18, 8)), "source": tipo.origen, "date": tipo.fecha.isoformat()})
        capture = QuoteSelectionContext.objects.create(actor=actor, kind="SEARCH", payload={
            "offers": offers, "fx_context_id": str(fx.pk) if fx else None})
        return capture, list(offers), str(fx.pk) if fx else None


def _owned_context(ref, actor, kind):
    try:
        return QuoteSelectionContext.objects.get(pk=_uuid(ref), actor_id=actor.pk, kind=kind)
    except QuoteSelectionContext.DoesNotExist:
        raise _OfertaNoDisponible("Referencia de captura no disponible para este usuario.") from None


def _fx_context(ref, actor, revision=None):
    if ref is None:
        return None, None
    key = _uuid(ref)
    if revision is not None and key == revision.exchange_context_id:
        return commercial_services._tipo(revision), key
    context = _owned_context(str(key), actor, "FX")
    data = context.payload
    return TipoCambio(Decimal(data["value"]), data["source"], datetime.fromisoformat(data["date"])), key


def _vat(data):
    _fields(data, {"vat_basis", "vat_rate"}, {"vat_basis"})
    if data["vat_basis"] == "NET" and "vat_rate" not in data:
        return None
    if data["vat_basis"] == "INCLUDED":
        return _decimal_text(data.get("vat_rate"), 5, 4, maximum=Decimal("1"))
    raise _EntradaInvalida("Base IVA NET o INCLUDED explícita; INCLUDED requiere vat_rate.")


def _price(data):
    _fields(data, {"amount", "currency", "vat_basis", "vat_rate"}, {"amount", "currency", "vat_basis"})
    if data["currency"] not in ("MXN", "USD"):
        raise _EntradaInvalida("Moneda MXN o USD requerida.")
    return _decimal_text(data["amount"], 18, 6), data["currency"], _vat({k: v for k, v in data.items() if k.startswith("vat_")})


def _commercial_items(items, actor, revision=None):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise _EntradaInvalida("Se requieren de 1 a 500 partidas.")
    old = {} if revision is None else {str(s.line_key): s for s in revision.snapshots.all()}
    output, keys = [], set()
    for item in items:
        _fields(item, {"line_key", "source", "quantity", "units", "pricing", "description", "cost"},
                {"line_key", "source", "quantity", "units", "pricing"})
        key = _uuid(item["line_key"])
        if key in keys:
            raise _EntradaInvalida("line_key duplicado.")
        keys.add(key)
        src = item["source"]
        _fields(src, {"type", "context_ref", "offer_ref", "name", "code", "brand", "model"}, {"type"})
        previous = old.get(str(key))
        if src["type"] == "EXISTING":
            _fields(src, {"type"})
            if previous is None:
                raise RevisionConflict("La partida no pertenece a esta revisión.")
            data = {f: getattr(previous, f) for f in commercial_services.PartidaComercialCapturada.__dataclass_fields__}
        elif previous is not None:
            raise RevisionConflict("Una captura existente se conserva con source.type=EXISTING.")
        elif src["type"] == "API":
            _fields(src, {"type", "context_ref", "offer_ref"}, {"context_ref", "offer_ref"})
            capture = _owned_context(src["context_ref"], actor, "SEARCH")
            captured = capture.payload["offers"].get(str(_uuid(src["offer_ref"])))
            if captured is None:
                raise _OfertaNoDisponible("La oferta no pertenece al contexto seleccionado.")
            if captured["vat"].get("vat_basis") == "UNKNOWN":
                raise RevisionConflict("Base IVA del proveedor no verificada; no se asume precio neto.")
            data = {k: captured[k] for k in ("name", "provider_code", "provider_name", "external_id", "brand", "model", "mpn", "stock", "es_mejor_precio")}
            data.update(source_kind="API", source_unit_price=Decimal(captured["price"]),
                source_currency=captured["currency"], cost_tax_rate_included=_vat(captured["vat"]))
        elif src["type"] == "MANUAL":
            _fields(src, {"type", "name", "code", "brand", "model"}, {"name"})
            data = {k: _texto_requerido(src.get(k, ""), k, 6000) for k in ("name", "code", "brand", "model")}
            data.update(source_kind="MANUAL", source_unit_price=None, source_currency=None)
        else:
            raise _EntradaInvalida("Origen de partida inválido.")
        if "cost" in item:
            if data["source_kind"] != "MANUAL":
                raise _EntradaInvalida("No se acepta costo API del navegador.")
            cost = (None, None, None) if item["cost"] is None else _price(item["cost"])
            data.update(zip(("source_unit_price", "source_currency", "cost_tax_rate_included"), cost))
        units = _fields(item["units"], {"source_label", "sale_label", "conversion_factor", "confirmed"},
                        {"source_label", "sale_label", "conversion_factor", "confirmed"})
        if units["confirmed"] is not True:
            raise _EntradaInvalida("Confirma las unidades y el factor de conversión.")
        data.update(source_unit_label=_texto_requerido(units["source_label"], "source_label", 80),
            sale_unit_label=_texto_requerido(units["sale_label"], "sale_label", 80),
            conversion_factor=_decimal_text(units["conversion_factor"], 18, 6, minimum=Decimal(".000001")))
        pricing = _fields(item["pricing"], {"mode", "margin_percent", "sale"}, {"mode"})
        data.update(pricing_mode=pricing["mode"], margin_percent=None, manual_unit_price=None,
                    manual_currency=None, manual_tax_rate_included=None)
        if pricing["mode"] == "MARGIN":
            _fields(pricing, {"mode", "margin_percent"})
            if previous is not None and "margin_percent" not in pricing:
                raise _EntradaInvalida("Edición requiere margen explícito.")
            data["margin_percent"] = _decimal_text(pricing.get("margin_percent", "35"), 4, 2, maximum=Decimal("99.99"))
        elif pricing["mode"] == "MANUAL":
            _fields(pricing, {"mode", "sale"}, {"sale"})
            data.update(zip(("manual_unit_price", "manual_currency", "manual_tax_rate_included"), _price(pricing["sale"])))
        else:
            raise _EntradaInvalida("Modo MARGIN o MANUAL requerido.")
        data.update(line_key=key, quantity=Decimal(_entero(item["quantity"], "quantity")),
            description=_texto_requerido(item.get("description", data.get("description", "")), "description", 6000))
        output.append(commercial_services.PartidaComercialCapturada(**data))
    return output


def _header_inputs(data, actor, revision=None):
    client = _cliente(data)
    client_data = {"business_name": client["client_name"], "rfc": client["client_rfc"], "email": client["client_email"]}
    for field in ("contact", "phone", "project"):
        client_data[field] = _texto_requerido(data.get("client_" + field, ""), field, 500)
    if revision is not None:
        required = {"issue_date", "valid_days", "tax_rate", "discount_percent", "fx_context_id"}
        if required - set(data):
            raise _EntradaInvalida("La edición requiere todos los campos comerciales.")
    try:
        if "issue_date" in data and (not isinstance(data["issue_date"], str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", data["issue_date"])):
            raise ValueError
        issue_date = date.fromisoformat(data["issue_date"]) if "issue_date" in data else timezone.localdate()
    except (ValueError, TypeError):
        raise _EntradaInvalida("issue_date debe tener formato YYYY-MM-DD.") from None
    tipo, context_id = _fx_context(data.get("fx_context_id"), actor, revision)
    return {"client": client_data, "conditions": client["conditions"], "valid_days": client["valid_days"],
        "issue_date": issue_date, "tax_rate": _decimal_text(data.get("tax_rate", str(TASA_IVA_INICIAL)), 5, 4, maximum=Decimal("1")),
        "discount_percent": _decimal_text(data.get("discount_percent", "0"), 5, 2, maximum=Decimal("100")),
        "tipo_cambio": tipo, "exchange_context_id": context_id}


def _request_once(actor, data, operation, target, action):
    actor = require_commercial_permission(actor)
    request_id = _uuid(data.get("request_id"))
    canonical = {"operation": operation, "target": target, "data": {k: v for k, v in data.items() if k != "request_id"}}
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
        default=lambda d: {"__json_decimal__": str(d)}, allow_nan=False).encode()).hexdigest()
    # Un lock por intención, NO User→Quote: evita invertir Quote→User de emisión.
    with transaction.atomic():
        if connection.vendor == "postgresql":
            lock = int.from_bytes(hashlib.sha256(f"quote-api:{actor.pk}:{request_id}".encode()).digest()[:8], "big", signed=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock])
        receipt = QuoteAPIRequest.objects.select_for_update().filter(actor=actor, request_id=request_id).first()
        if receipt:
            if receipt.operation != operation or receipt.fingerprint != fingerprint:
                raise RevisionConflict("request_id pertenece a otra intención.")
            result = receipt.revision if receipt.revision_id else receipt.context
            replay = True
        else:
            if QuoteRevision.objects.filter(request_id=request_id, quote__advisor=actor).exists():
                raise RevisionConflict("request_id pertenece a una operación previa sin recibo API.")
            result = action(actor, request_id)
            QuoteAPIRequest.objects.create(actor=actor, request_id=request_id, operation=operation,
                fingerprint=fingerprint, revision=result if isinstance(result, QuoteRevision) else None,
                context=result if isinstance(result, QuoteSelectionContext) else None)
            replay = False
        if isinstance(result, QuoteRevision):
            quote, result = commercial_services._locked(result.pk, actor)
            commercial_services._commercial_actor(quote, actor)
            output = {**_resumen(result, quote.folio), "schema_version": result.schema_version,
                      "state_fingerprint": result.state_fingerprint}
        else:
            if result.payload.get("source") != "syscom":
                require_commercial_permission(actor, FX)
            if result.actor_id != actor.pk:
                raise PermissionDenied
            output = {"fx_context_id": str(result.pk), **result.payload}
        return {**output, "replayed": replay}


def _commercial_create(actor, data):
    def create(current, request_id):
        kwargs = _header_inputs(data, current)
        parts = _commercial_items(data.get("items"), current)
        # 5A: la empresa se elige al crear y queda fija: su prefijo va en el folio.
        clave = data.get("company_key", EMPRESA_PRINCIPAL)
        empresa = _empresa(clave)
        quote = Quote(advisor=current, client_name=kwargs["client"]["business_name"],
            client_rfc=kwargs["client"]["rfc"], client_email=kwargs["client"]["email"],
            issue_date=kwargs["issue_date"], valid_days=kwargs["valid_days"],
            notes=_texto_requerido(data.get("notes", ""), "notes", 6000))
        quote.folio_prefix = _prefijo_folio(clave)
        quote.full_clean(exclude=["folio"])
        # Reintentar únicamente el choque de folio en un savepoint propio.
        for attempt in range(_INTENTOS_FOLIO):
            try:
                with transaction.atomic():
                    quote.save(force_insert=True)
                break
            except IntegrityError:
                if attempt == _INTENTOS_FOLIO - 1:
                    raise
                quote.pk, quote.folio = None, ""
        return commercial_services.crear_borrador_comercial(quote.pk, parts, actor=current,
            company=empresa, request_id=request_id, **kwargs)
    return _request_once(actor, data, "CREATE", None, create)


def _get_commercial(folio, number, actor):
    revision_id = QuoteRevision.objects.values_list("pk", flat=True).get(quote__folio=folio, number=number)
    quote, revision = commercial_services._locked(revision_id, actor)
    commercial_services._commercial_actor(quote, actor)
    return quote, revision


def _emission_target(folio, number, actor):
    with transaction.atomic():
        return _get_commercial(folio, number, actor)[1]


def _commercial_edit(folio, number, actor, data, *, preview=False):
    with transaction.atomic():
        quote, revision = _get_commercial(folio, number, actor)
        if revision.schema_version != 2:
            _legacy_edit_disabled()
        kwargs = _header_inputs(data, actor, revision)
        parts = _commercial_items(data.get("items"), actor, revision)
        fn = commercial_services.previsualizar_edicion_comercial if preview else commercial_services.guardar_borrador_comercial
        result = fn(revision.pk, parts, actor=actor, expected_version=_entero(data.get("edit_version"), "edit_version"),
            company=revision.header["company"], **kwargs)
        if preview:
            candidate = QuoteRevision(discount_percent=kwargs["discount_percent"])
            result["warnings"] = _warning_states(result["resultado"], candidate)
        return _wire(result) if preview else {**_resumen(result, quote.folio), "state_fingerprint": result.state_fingerprint}


def _tipo_cambio_con_autor(revision):
    """Lienzo fila 13: de dónde salió (SYSCOM o a mano) y, si fue a mano, quién lo capturó. Solo interno."""
    tipo = _tipo_cambio(commercial_services._tipo(revision))
    if tipo and tipo["origen"] != "syscom" and revision.exchange_context_id:
        contexto = QuoteSelectionContext.objects.select_related("actor").filter(pk=revision.exchange_context_id).first()
        if contexto and contexto.actor:
            tipo["capturado_por"] = contexto.actor.get_full_name().strip() or contexto.actor.get_username()
    return tipo


def _exact_detail(folio, number, actor):
    with transaction.atomic():
        quote, revision = _get_commercial(folio, number, actor)
        snapshots = list(revision.snapshots.all())
        output = {**_resumen(revision, folio), "schema_version": revision.schema_version,
            "calculation_version": revision.calculation_version, "state_fingerprint": revision.state_fingerprint,
            "client": revision.header["client"], "conditions": revision.header["conditions"],
            "company": {"key": revision.header["company"].get("key", EMPRESA_PRINCIPAL), "name": revision.header["company"]["name"]},
            "issue_date": revision.issue_date.isoformat(), "valid_days": revision.valid_days,
            "tax_rate": revision.tax_rate, "discount_percent": revision.discount_percent,
            "fx_context_id": revision.exchange_context_id,
            "tipo_cambio": _tipo_cambio_con_autor(revision),
            "capabilities": _capabilities(actor),
            "emission_enabled": bool(settings.COTIZACIONES_EMISION_HABILITADA and revision.schema_version == 2),
            "items": [{f: getattr(s, f) for f in commercial_services.PartidaComercialCapturada.__dataclass_fields__}
                      if revision.schema_version == 2 else {"name": s.name, "quantity": s.quantity,
                          "source_unit_price": s.source_unit_price, "source_currency": s.source_currency} for s in snapshots],
            "confirmations": list(revision.commercial_confirmations.values("edit_version", "state_fingerprint", "warning_code", "scope_ref", "confirmed_by_id", "confirmed_at"))}
        if revision.schema_version == 2:
            # 23A: cuándo se capturó el precio de cada partida, para «Revisar precios».
            for item, snapshot in zip(output["items"], snapshots, strict=True):
                item["captured_at"] = snapshot.captured_at.isoformat()
            if revision.status == "DRAFT":
                output["preview"] = commercial_services.previsualizar_revision_comercial(revision.pk, actor=actor, expected_version=revision.edit_version)
                output["preview"]["warnings"] = _warning_states(output["preview"]["resultado"], revision, quote)
            else:
                output["sealed"] = {f: getattr(revision, f) for f in ("sale_subtotal", "discount_amount", "known_cost_total", "costs_complete", "gross_profit")}
                output["cliente"] = commercial_services.contexto_revision(revision)
                for item, snapshot in zip(output["items"], snapshots, strict=True):
                    item.update(sale_amount=snapshot.sale_amount, cost_amount=snapshot.cost_amount)
        return _wire(output)


def _warning_states(result, revision, quote=None):
    records = [] if quote is None else list(revision.commercial_confirmations.filter(
        edit_version=revision.edit_version, state_fingerprint=revision.state_fingerprint).select_related("confirmed_by"))
    output = []
    for warning in result.advertencias:
        code, scope = str(warning.codigo), warning.referencia or ""
        if not warning.requiere_confirmacion:
            output.append({"code": code, "line_key": scope or None, "requires_confirmation": False,
                           "required_capability": None, "confirmed": False})
            continue
        capability = commercial_services._warning_permission(code, scope, revision)
        confirmed = False
        for record in records:
            if (record.warning_code, record.scope_ref) != (code, scope):
                continue
            try:
                current = require_commercial_permission(record.confirmed_by, capability)
                authorize(quote, current)
            except PermissionDenied:
                continue
            confirmed = True
            break
        output.append({"code": code, "line_key": scope or None, "requires_confirmation": True,
                       "required_capability": capability, "confirmed": confirmed})
    return output


def _commercial_clone(folio, number, actor, data):
    _schema_v2(data)
    def clone(current, request_id):
        quote, revision = _get_commercial(folio, number, current)
        if "source_edit_version" in data and _entero(data["source_edit_version"], "source_edit_version") != revision.edit_version:
            raise RevisionConflict("La revisión de origen cambió; recarga antes de crear otra revisión.")
        if revision.schema_version == 2:
            _fields(data, {"request_id", "schema_version", "source_edit_version"})
            parts = [commercial_services.PartidaComercialCapturada(**p) for p in commercial_services._commercial_stored(revision)]
            tax, discount, tipo, fx_id = revision.tax_rate, revision.discount_percent, commercial_services._tipo(revision), revision.exchange_context_id
        else:
            parts = _commercial_items(data.get("items"), current)
            tax = _decimal_text(data.get("tax_rate"), 5, 4, maximum=Decimal("1"))
            discount = _decimal_text(data.get("discount_percent"), 5, 2, maximum=Decimal("100"))
            tipo, fx_id = _fx_context(data.get("fx_context_id"), current)
        return commercial_services.crear_revision_comercial(revision.pk, parts, actor=current, request_id=request_id,
            tax_rate=tax, discount_percent=discount, tipo_cambio=tipo, exchange_context_id=fx_id)
    return _request_once(actor, data, "CLONE", [folio, number], clone)


@_api("POST")
async def preview_nueva(request):
    data = _leer_json(request, (_CAMPOS_ALTA | _COMMERCIAL_HEADER | {"schema_version", "company_key"}) - {"request_id", "notes"})
    _schema_v2(data)
    def preview(actor):
        actor = require_commercial_permission(actor)
        kwargs = _header_inputs(data, actor)
        result = commercial_services.previsualizar_nueva_cotizacion(_commercial_items(data.get("items"), actor),
            actor=actor, company=_empresa(data.get("company_key", EMPRESA_PRINCIPAL)), **kwargs)
        result["warnings"] = _warning_states(result["resultado"], QuoteRevision(discount_percent=kwargs["discount_percent"]))
        return _wire(result)
    output = await sync_to_async(preview, thread_sensitive=True)(await request.auser())
    return JsonResponse({"success": True, "data": output})


@_api("POST")
async def preview_edicion(request, folio, numero):
    data = _leer_json(request, _CAMPOS_EDICION | _COMMERCIAL_HEADER)
    output = await sync_to_async(_commercial_edit, thread_sensitive=True)(folio, numero, await request.auser(), data, preview=True)
    return JsonResponse({"success": True, "data": output})


@_api("POST")
async def confirmar_comercial(request, folio, numero):
    data = _leer_json(request, {"edit_version", "state_fingerprint", "warnings"})
    def confirm(actor):
        with transaction.atomic():
            quote, revision = _get_commercial(folio, numero, actor)
            warnings = data.get("warnings")
            if not isinstance(warnings, list) or len(warnings) > MAX_ITEMS * 3 + 5:
                raise _EntradaInvalida("warnings debe ser una lista acotada.")
            entries = []
            for warning in warnings:
                _fields(warning, {"code", "line_key"}, {"code", "line_key"})
                entries.append((warning["code"], str(_uuid(warning["line_key"])) if warning["line_key"] is not None else ""))
            records = commercial_services.confirmar_advertencias_comerciales(revision.pk, entries, actor=actor,
                expected_version=_entero(data.get("edit_version"), "edit_version"), state_fingerprint=_hash_input(data.get("state_fingerprint")))
            return {"confirmed": [r.pk for r in records], "edit_version": revision.edit_version, "state_fingerprint": revision.state_fingerprint}
    return JsonResponse({"success": True, "data": await sync_to_async(confirm, thread_sensitive=True)(await request.auser())})


@_api("POST")
async def crear_contexto_fx(request):
    """{"value"}: capturado a mano (permiso de tipo de cambio). {"use_syscom": true}: el de SYSCOM de hoy,
    que consulta el servidor; lo puede usar cualquiera con acceso comercial (lienzo fila 13)."""
    data = _leer_json(request, {"request_id", "value", "use_syscom"})
    syscom = None
    if "use_syscom" in data:
        if data["use_syscom"] is not True or "value" in data:
            raise _EntradaInvalida("Envía value o use_syscom.")
        syscom, _ = await tipo_cambio_sugerido()
        if syscom is None:
            raise _SinTipoCambioSyscom()
    def operation(actor):
        require_commercial_permission(actor, VIEW_COSTS if syscom is not None else FX)
        def create(current, request_id):
            if syscom is not None:
                return QuoteSelectionContext.objects.create(actor=current, kind="FX", payload={
                    "value": _texto(decimal_limit(syscom, 18, 8)), "source": "syscom", "date": timezone.now().isoformat()})
            value = _decimal_text(data.get("value"), 18, 8, minimum=Decimal(".00000001"))
            return QuoteSelectionContext.objects.create(actor=current, kind="FX", payload={
                "value": _texto(value), "source": "manual", "date": timezone.now().isoformat()})
        return _request_once(actor, data, "FX", None, create)
    return JsonResponse({"success": True, "data": await sync_to_async(operation, thread_sensitive=True)(await request.auser())}, status=201)


def _capabilities(actor):
    current = require_commercial_permission(actor)
    return {p: current.has_perm("quotes." + p) for p in sorted(ADJUSTMENTS | {VIEW_COSTS, MANAGE})}


@_api("GET")
async def capacidades_comerciales(request):
    actor = await request.auser()
    return JsonResponse({"success": True, "actor_id": actor.pk,
                         "data": await sync_to_async(_capabilities, thread_sensitive=True)(actor)})


@_api("GET PUT")
async def tipo_cambio_sesion(request):
    """15A/16A: el tipo de cambio de la sesión (uno solo para comparador, cotizador y menú) y el de SYSCOM.

    Lienzo fila 13: el de SYSCOM de hoy se pone solo para todos. PUT {"value": "18.70"} lo guarda como manual
    (solo con permiso de tipo de cambio); {"use_syscom": true} vuelve al de SYSCOM. Solo vive en la sesión:
    no cambia ninguna cotización ni llega al cliente.
    """
    puede = await sync_to_async(puede_cambiar_tipo_cambio, thread_sensitive=True)(await request.auser())
    recordado = await request.session.aget(CLAVE_TIPO_CAMBIO)
    if request.method == "PUT":
        data = _leer_json(request, {"value", "use_syscom"})
        if data == {"use_syscom": True}:
            nuevo = await tipo_cambio_automatico(
                request.session, recordado if recordado and recordado.get("origen") == "syscom" else None)
            if nuevo is None:
                raise _SinTipoCambioSyscom()
            recordado = nuevo
        elif set(data) == {"value"}:
            if not puede:
                return JsonResponse({"success": False, "error": SOLO_QUIEN_CAMBIA_TC,
                                     "code": "FORBIDDEN", "details": {}}, status=403)
            _decimal_text(data["value"], 18, 8, minimum=Decimal(".00000001"))  # mismos límites que el cotizador
            tipo = TipoCambio(Decimal(data["value"]), "manual", timezone.now())
            # El mismo valor (18.54 = 18.54000000) conserva su hora y origen, como en el comparador.
            if not (recordado and Decimal(recordado["valor"]) == tipo.valor):
                recordado = {"valor": f"{tipo.valor:f}", "fecha": tipo.fecha.isoformat(), "origen": "manual"}
                await request.session.aset(CLAVE_TIPO_CAMBIO, recordado)
        else:
            raise _EntradaInvalida("Envía value o use_syscom.")
    elif not puede or recordado is None or recordado.get("origen") == "syscom":
        recordado = await tipo_cambio_automatico(request.session, recordado)
    syscom, fallo = await tipo_cambio_sugerido()
    return JsonResponse({"success": True, "data": {
        "session": {"value": recordado["valor"], "date": recordado["fecha"], "source": recordado.get("origen", "manual"),
                    "confirmed": recordado.get("confirmado")}
        if recordado else None,
        "syscom": {"value": f"{syscom:f}", "label": ETIQUETAS_TIPO_CAMBIO_SYSCOM.get(settings.SYSCOM_TIPO_CAMBIO_CAMPO, "")}
        if syscom is not None else None,
        "syscom_failed": fallo, "can_change": puede}})


@_api("GET PATCH")
async def permisos_comerciales(request, user_id):
    data = _leer_json(request, {"capability", "enabled"}) if request.method == "PATCH" else None
    def permissions(actor):
        require_commercial_permission(actor, MANAGE)
        target = get_user_model().objects.get(pk=user_id)
        require_commercial_permission(target)
        if data is not None:
            set_commercial_permission(user_id, data.get("capability"), actor=actor, enabled=data.get("enabled"))
        return {"user_id": user_id, "capabilities": _capabilities(target)}
    return JsonResponse({"success": True, "data": await sync_to_async(permissions, thread_sensitive=True)(await request.auser())})
