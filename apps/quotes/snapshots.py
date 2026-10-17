"""Servicios M8. M7 integra estas operaciones; M9 crea revisiones, nunca pisa emisiones.

Actores autenticados/activos: asesor propietario o staff. No se consulta raw ni
proveedores, ni se copian originales desde los campos legacy de dos decimales.
"""

import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, timedelta, timezone as dt_timezone
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

from asgiref.sync import sync_to_async
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.providers.contracts import ProviderOffer

from .commercial import MARGEN_INICIAL, TASA_IVA_INICIAL
from .comparison import OfertaComparada, TipoCambio
from .models import Quote, QuoteCommercialConfirmation, QuoteRevision, QuoteSnapshot
from .snapshot_values import MAX_ITEMS, MONEY_CONTEXT, amount, decimal_limit

logger = logging.getLogger(__name__)


class RevisionConflict(ValidationError):
    """El borrador cambió o ya está emitido; M7 debe recargarlo, no sobrescribir."""


class ArchivedPDFError(Exception):
    """Falta el archivo original o no coincide con su huella; restaurar respaldo."""


@dataclass(frozen=True, slots=True)
class PartidaCapturada:
    name: str
    source_unit_price: Decimal
    source_currency: str
    quantity: Decimal
    code: str = ""
    description: str = ""
    provider_code: str = ""
    provider_name: str = ""
    external_id: str = ""
    brand: str = ""
    model: str = ""
    mpn: str = ""
    stock: int | None = None
    es_mejor_precio: bool = False

    @classmethod
    def desde_oferta(cls, oferta: ProviderOffer, cantidad: Decimal, *, nombre_proveedor: str):
        if not isinstance(oferta, ProviderOffer):
            raise TypeError("Se requiere ProviderOffer.")
        return cls(name=oferta.name, source_unit_price=oferta.price, source_currency=oferta.currency,
                   quantity=cantidad, code=oferta.external_id, provider_code=oferta.provider_code,
                   provider_name=nombre_proveedor, external_id=oferta.external_id, stock=oferta.stock,
                   brand=oferta.brand or "", model=oferta.model or "", mpn=oferta.mpn or "")

    @classmethod
    def desde_comparada(cls, comparada: OfertaComparada, cantidad: Decimal, *, nombre_proveedor: str):
        if not isinstance(comparada, OfertaComparada):
            raise TypeError("Se requiere OfertaComparada.")
        datos = asdict(cls.desde_oferta(comparada.oferta, cantidad, nombre_proveedor=nombre_proveedor))
        datos["es_mejor_precio"] = comparada.es_mejor_precio
        return cls(**datos)


@dataclass(frozen=True, slots=True)
class PartidaComercialCapturada(PartidaCapturada):
    """Contrato v2 interno: cantidad Decimal entera, identidad estable explícita.

    Una entrada API procede de captura confiable del consumidor; esta dataclass no
    verifica una petición al proveedor ni firma precios enviados por el navegador.
    None en precio/moneda de costo solo representa un concepto MANUAL desconocido.
    """

    source_unit_price: Decimal | None
    source_currency: str | None
    line_key: UUID | None = None
    source_kind: str = "MANUAL"
    source_unit_label: str = ""
    sale_unit_label: str = ""
    conversion_factor: Decimal = Decimal("1")
    cost_tax_rate_included: Decimal | None = None
    pricing_mode: str = "MARGIN"
    margin_percent: Decimal | None = None
    manual_unit_price: Decimal | None = None
    manual_currency: str | None = None
    manual_tax_rate_included: Decimal | None = None


def authorize(quote, actor):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("Se requiere un usuario activo autenticado.")
    if not actor.is_staff and actor.pk != quote.advisor_id:
        raise PermissionDenied("No tienes acceso a esta cotización.")


def _text(value, *, required=False, limit=6000):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValidationError("Texto vacío, inválido o demasiado largo.")
    return value


def _header(quote, company, conditions, client):
    if not isinstance(company, dict) or not isinstance(client, dict) or not isinstance(conditions, (list, tuple)):
        raise ValidationError("Cabecera estructurada requerida.")
    company_keys = ("name", "rfc", "tagline", "phone", "email")
    client_keys = ("business_name", "rfc", "contact", "email", "phone", "project")
    if set(company) - set(company_keys) - {"advisor"} or set(client) - set(client_keys):
        raise ValidationError("Campos desconocidos en la cabecera.")
    if len(conditions) > 50:
        raise ValidationError("Máximo 50 condiciones.")
    c = {key: _text(company.get(key, ""), required=key == "name", limit=500) for key in company_keys}
    c["advisor"] = quote.advisor.get_full_name() or quote.advisor.get_username()
    return {
        "folio": quote.folio,
        "company": c,
        "client": {key: _text(client.get(key, ""), required=key == "business_name", limit=500) for key in client_keys},
        "conditions": [_text(cond, required=True) for cond in conditions],
    }


def _parts(partidas, tipo):
    if tipo is not None and not isinstance(tipo, TipoCambio):
        raise TypeError("Se requiere TipoCambio con origen y fecha.")
    if tipo is not None:
        decimal_limit(tipo.valor, 18, 8, minimum=Decimal("0.00000001"))
        _text(tipo.origen, required=True, limit=120)
    salida = []
    for partida in partidas:
        if len(salida) >= MAX_ITEMS:
            raise ValidationError(f"Máximo {MAX_ITEMS} partidas por revisión.")
        if not isinstance(partida, PartidaCapturada):
            raise TypeError("Se requiere PartidaCapturada.")
        datos = asdict(partida)
        for key in ("name", "code", "description", "provider_code", "provider_name", "external_id", "brand", "model", "mpn"):
            limit = 80 if key == "provider_code" else 255 if key == "provider_name" else 6000
            _text(datos[key], required=key == "name", limit=limit)
        if datos["source_currency"] not in {"MXN", "USD"}:
            raise ValidationError("Solo MXN y USD están soportados.")
        if datos["source_currency"] == "USD" and tipo is None:
            raise ValidationError("USD requiere un tipo de cambio explícito.")
        datos["source_unit_price"] = decimal_limit(datos["source_unit_price"], 18, 6)
        datos["quantity"] = decimal_limit(datos["quantity"], 10, 2, minimum=Decimal("0.01"))
        if datos["stock"] is not None and (type(datos["stock"]) is not int or not 0 <= datos["stock"] <= 9223372036854775807):
            raise ValidationError("Stock fuera de rango o no entero.")
        if type(datos["es_mejor_precio"]) is not bool:
            raise ValidationError("es_mejor_precio debe ser bool.")
        if bool(datos["provider_code"]) != bool(datos["external_id"]) or (datos["provider_code"] and not datos["provider_name"].strip()):
            raise ValidationError("Proveedor e identificador externo incoherentes.")
        salida.append(datos)
    if not salida:
        raise ValidationError("Una revisión requiere al menos una partida.")
    return salida


def _fingerprint(header, parts, tipo, tax_rate, issue_date, valid_days, based_on):
    datos = {"header": header, "parts": parts, "tipo": (
        {"valor": str(decimal_limit(tipo.valor, 18, 8)), "origen": tipo.origen, "fecha": tipo.fecha.isoformat()}
        if tipo else None), "tax": str(tax_rate), "date": issue_date.isoformat(), "days": valid_days, "base": based_on}
    return hashlib.sha256(json.dumps(datos, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _capture(quote, parts, *, actor, company, conditions, client, tipo_cambio, tax_rate, issue_date,
             valid_days, request_id, based_on=None):
    authorize(quote, actor)
    parts = _parts(parts, tipo_cambio)
    header = _header(quote, company, conditions, client)
    tax_rate = decimal_limit(tax_rate, 5, 4, maximum=Decimal("1"))
    if type(issue_date) is not date or type(valid_days) is not int or not 1 <= valid_days <= 3650:
        raise ValidationError("Fecha y vigencia inválidas (1–3650 días).")
    _valid_until(issue_date, valid_days)
    if not isinstance(request_id, UUID):
        raise TypeError("request_id debe ser UUID; reutilízalo en reintentos.")
    fingerprint = _fingerprint(header, parts, tipo_cambio, tax_rate, issue_date, valid_days, based_on)
    previous = quote.revisions.filter(request_id=request_id).first()
    if previous:
        if previous.request_fingerprint != fingerprint:
            raise RevisionConflict("request_id ya fue utilizado con datos diferentes.")
        return previous
    number = (quote.revisions.aggregate(number=Max("number"))["number"] or 0) + 1
    revision = QuoteRevision.objects.create(
        quote=quote, number=number, based_on_id=based_on, request_id=request_id, request_fingerprint=fingerprint,
        header=header, tax_rate=tax_rate, issue_date=issue_date, valid_days=valid_days,
        exchange_rate=decimal_limit(tipo_cambio.valor, 18, 8) if tipo_cambio else None,
        exchange_rate_source=tipo_cambio.origen if tipo_cambio else "",
        exchange_rate_date=tipo_cambio.fecha if tipo_cambio else None,
    )
    for position, datos in enumerate(parts, 1):
        QuoteSnapshot.objects.create(revision=revision, position=position, **datos)
    return revision


def crear_borrador(quote_id, partidas, *, actor, company, conditions, tipo_cambio=None,
                   client=None, tax_rate=None, issue_date=None, valid_days=None, request_id=None):
    """Captura originales explícitos. No convierte QuoteItem legacy automáticamente."""
    with transaction.atomic():
        quote = Quote.objects.select_for_update(of=("self",)).select_related("advisor").get(pk=quote_id)
        return _capture(
            quote, partidas, actor=actor, company=company, conditions=conditions,
            client=client if client is not None else {"business_name": quote.client_name, "rfc": quote.client_rfc, "email": quote.client_email},
            tipo_cambio=tipo_cambio, tax_rate=quote.tax_rate if tax_rate is None else tax_rate,
            issue_date=quote.issue_date if issue_date is None else issue_date,
            valid_days=quote.valid_days if valid_days is None else valid_days,
            request_id=request_id if request_id is not None else uuid4(),
        )


def _locked(revision_id, actor):
    quote_id = QuoteRevision.objects.values_list("quote_id", flat=True).get(pk=revision_id)
    quote = Quote.objects.select_for_update(of=("self",)).select_related("advisor").get(pk=quote_id)
    authorize(quote, actor)
    revision = QuoteRevision.objects.select_for_update().get(pk=revision_id)
    return quote, revision


def _tipo(revision):
    if revision.exchange_rate is None:
        return None
    return TipoCambio(revision.exchange_rate, revision.exchange_rate_source, revision.exchange_rate_date)


def _valid_until(issue_date, valid_days):
    try:
        return issue_date + timedelta(days=valid_days)
    except OverflowError as exc:
        raise ValidationError("La vigencia excede el rango de fechas.") from exc


def guardar_borrador(revision_id, partidas, *, actor, expected_version, company, conditions,
                     client, tipo_cambio, tax_rate, issue_date, valid_days):
    """Reemplazo completo con control optimista; M7 debe enviar edit_version leído."""
    with transaction.atomic():
        quote, revision = _locked(revision_id, actor)
        if revision.schema_version != 1:
            raise RevisionConflict("Usa guardar_borrador_comercial para una revisión v2.")
        if revision.status != QuoteRevision.Status.DRAFT or type(expected_version) is not int or expected_version != revision.edit_version:
            raise RevisionConflict("El borrador cambió o ya fue emitido.")
        parts = _parts(partidas, tipo_cambio)
        header = _header(quote, company, conditions, client)
        if type(issue_date) is not date or type(valid_days) is not int or not 1 <= valid_days <= 3650:
            raise ValidationError("Fecha/vigencia inválidas.")
        _valid_until(issue_date, valid_days)
        for snapshot in revision.snapshots.all():
            snapshot.delete()
        revision.header = header
        revision.exchange_rate = decimal_limit(tipo_cambio.valor, 18, 8) if tipo_cambio else None
        revision.exchange_rate_source = tipo_cambio.origen if tipo_cambio else ""
        revision.exchange_rate_date = tipo_cambio.fecha if tipo_cambio else None
        revision.tax_rate = decimal_limit(tax_rate, 5, 4, maximum=Decimal("1"))
        revision.issue_date, revision.valid_days = issue_date, valid_days
        revision.edit_version += 1
        revision.save()
        for position, datos in enumerate(parts, 1):
            QuoteSnapshot.objects.create(revision=revision, position=position, **datos)
        return revision


def crear_revision(revision_id, *, actor, request_id=None):
    """Clona una emisión como borrador; repetir request_id no crea otra versión."""
    with transaction.atomic():
        quote, previous = _locked(revision_id, actor)
        if previous.schema_version != 1:
            raise RevisionConflict("Usa crear_revision_comercial con entradas explícitas para v2.")
        if previous.status != QuoteRevision.Status.ISSUED:
            raise RevisionConflict("Solo una emisión puede originar una nueva revisión.")
        if request_id is not None:
            if not isinstance(request_id, UUID):
                raise TypeError("request_id debe ser UUID.")
            existing = quote.revisions.filter(request_id=request_id).first()
            if existing:
                if existing.based_on_id != previous.pk:
                    raise RevisionConflict("request_id pertenece a otra operación.")
                return existing
        parts = [PartidaCapturada(**{field: getattr(s, field) for field in PartidaCapturada.__dataclass_fields__})
                 for s in previous.snapshots.all()]
        return _capture(quote, parts, actor=actor, company=previous.header["company"],
                        conditions=previous.header["conditions"], client=previous.header["client"],
                        tipo_cambio=_tipo(previous), tax_rate=previous.tax_rate,
                        issue_date=timezone.localdate(), valid_days=previous.valid_days,
                        request_id=request_id if request_id is not None else uuid4(), based_on=previous.pk)


def contexto_revision(revision):
    """Contexto del renderer, sin datos de Product/Provider vigentes."""
    if revision.schema_version != 1:
        raise ValidationError("Versión de snapshot no soportada.")
    header = deepcopy(revision.header)
    items = []
    with localcontext(MONEY_CONTEXT):
        for snapshot in revision.snapshots.all():
            unit = snapshot.source_unit_price * (revision.exchange_rate if snapshot.source_currency == "USD" else Decimal("1"))
            line_amount = snapshot.amount if revision.status == QuoteRevision.Status.ISSUED else amount(unit * snapshot.quantity)
            items.append({"code": snapshot.code or "—", "name": snapshot.name, "description": snapshot.description,
                          "provider": snapshot.provider_name or "Concepto manual", "qty": snapshot.quantity,
                          "unit_price": unit, "amount": line_amount, "stock": snapshot.stock,
                          "source_unit_price": snapshot.source_unit_price, "source_currency": snapshot.source_currency})
        subtotal = revision.subtotal if revision.status == QuoteRevision.Status.ISSUED else amount(sum((i["amount"] for i in items), Decimal("0")))
        tax = revision.tax if revision.status == QuoteRevision.Status.ISSUED else amount(subtotal * revision.tax_rate)
        total = revision.total if revision.status == QuoteRevision.Status.ISSUED else amount(subtotal + tax)
        tax_label = f"{revision.tax_rate * 100:g}%"
    return {**header, "revision": revision.number,
            "issue_date": revision.issue_date.strftime("%d/%m/%Y"),
            "valid_until": _valid_until(revision.issue_date, revision.valid_days).strftime("%d/%m/%Y"),
            "validity_label": f"{revision.valid_days} días", "items": items,
            "subtotal": subtotal, "iva": tax, "total": total, "iva_rate_label": tax_label,
            "document_label": "BORRADOR — SIN EMITIR" if revision.status == QuoteRevision.Status.DRAFT else "COTIZACIÓN EMITIDA",
            "exchange_rate_label": (f"1 USD = {revision.exchange_rate:f} MXN; {revision.exchange_rate_source}; "
                                    f"{revision.exchange_rate_date.isoformat()}" if revision.exchange_rate is not None else "Sin conversión; precios de origen MXN"),
            "precision_units": True}


def leer_pdf_emitido(revision):
    if revision.status != QuoteRevision.Status.ISSUED:
        raise RevisionConflict("La revisión no está emitida.")
    try:
        with revision.pdf.storage.open(revision.pdf.name, "rb") as file:
            data = file.read()
    except (OSError, ValueError) as exc:
        raise ArchivedPDFError("PDF emitido no disponible; restaurar respaldo.") from exc
    if hashlib.sha256(data).hexdigest() != revision.pdf_sha256:
        raise ArchivedPDFError("El PDF archivado no coincide con su huella.")
    return data


def emitir_revision(revision_id, *, actor, expected_version):
    """Transacción propia: no envolver en otra atomic real ni enviar correo dentro.

    El archivo y PostgreSQL no son una única transacción. Un fallo recuperable
    elimina solo el archivo cuyo save terminó en esta operación. Un crash o save
    parcial puede dejar un huérfano privado; nunca publicar antes del commit.
    Si falla el commit, se conserva el archivo: el resultado de un corte de
    conexión puede ser incierto y borrarlo podría destruir una emisión real.
    """
    saved_name = None
    ready_to_commit = False
    storage = QuoteRevision._meta.get_field("pdf").storage
    try:
        with transaction.atomic(durable=True):
            quote, revision = _locked(revision_id, actor)
            if type(expected_version) is not int or expected_version != revision.edit_version:
                raise RevisionConflict("El borrador cambió; recarga antes de emitir.")
            if revision.status == QuoteRevision.Status.ISSUED:
                leer_pdf_emitido(revision)
                return revision
            if revision.schema_version != 1:
                raise RevisionConflict("Emisión comercial v2 pendiente de integración P3; no se genera PDF a costo.")
            snapshots = list(revision.snapshots.all())
            if not snapshots:
                raise ValidationError("No se emite una revisión vacía.")
            context = contexto_revision(revision)
            context["document_label"] = "COTIZACIÓN EMITIDA"
            from .pdf import generate_quote_pdf

            data = generate_quote_pdf(context)
            if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
                raise ValidationError("El generador no produjo un PDF válido.")
            revision.pdf.save("emision.pdf", ContentFile(data), save=False)
            saved_name = revision.pdf.name
            revision.pdf_sha256 = hashlib.sha256(data).hexdigest()
            with storage.open(saved_name, "rb") as stored:
                if hashlib.sha256(stored.read()).hexdigest() != revision.pdf_sha256:
                    raise ArchivedPDFError("La escritura del PDF no conservó los bytes generados.")
            revision.subtotal, revision.tax, revision.total = context["subtotal"], context["iva"], context["total"]
            for snapshot, item in zip(snapshots, context["items"], strict=True):
                snapshot.amount = item["amount"]
                snapshot.save(_seal=True)
            revision.status = QuoteRevision.Status.ISSUED
            revision.issued_at = timezone.now()
            revision.save(_seal=True)
            if quote.status == Quote.Status.DRAFT:
                quote.status = Quote.Status.ISSUED
                quote.save(update_fields=["status", "updated_at"])
            ready_to_commit = True
        return revision
    except Exception:
        if saved_name:
            if ready_to_commit:
                logger.error("M8: commit no confirmado; archivo conservado. Verificar BD y almacenamiento antes de recuperar.")
            else:
                try:
                    storage.delete(saved_name)
                except OSError:
                    logger.error("M8: no se pudo limpiar un archivo de una emisión fallida; revisar huérfanos.")
        raise


def obtener_documento(folio, *, actor, number=None, allow_draft=True):
    """Número explícito o última emitida; si no hay emisiones, último borrador."""
    from .pdf import generate_quote_pdf, get_quote_context, safe_filename

    # El contexto debe corresponder a UNA versión: impedir lectura de cabecera
    # anterior con partidas nuevas mientras otro hilo guarda/emite el borrador.
    with transaction.atomic():
        quote = Quote.objects.select_for_update().get(folio=folio)
        authorize(quote, actor)
        revisions = quote.revisions.select_for_update()
        if number is not None:
            if type(number) is not int or not 1 <= number <= 2147483647:
                raise ValidationError("Número de revisión inválido.")
            revision = revisions.get(number=number)
        else:
            revision = revisions.filter(status=QuoteRevision.Status.ISSUED).order_by("-number").first()
            revision = revision or revisions.order_by("-number").first()
        context = get_quote_context(folio) if revision is None else contexto_revision(revision)
    if revision is None:
        return context, generate_quote_pdf(context), safe_filename(folio)
    filename = safe_filename(f"{context['folio']}_r{revision.number}")
    if revision.status == QuoteRevision.Status.ISSUED:
        return context, leer_pdf_emitido(revision), filename
    if not allow_draft:
        raise RevisionConflict("Emite la revisión antes de enviarla por correo.")
    return context, generate_quote_pdf(context), filename


async def acrear_borrador(*args, **kwargs):
    return await sync_to_async(crear_borrador, thread_sensitive=True)(*args, **kwargs)


async def aguardar_borrador(*args, **kwargs):
    return await sync_to_async(guardar_borrador, thread_sensitive=True)(*args, **kwargs)


async def acrear_revision(*args, **kwargs):
    return await sync_to_async(crear_revision, thread_sensitive=True)(*args, **kwargs)


async def aemitir_revision(*args, **kwargs):
    return await sync_to_async(emitir_revision, thread_sensitive=True)(*args, **kwargs)


async def aobtener_documento(*args, **kwargs):
    return await sync_to_async(obtener_documento, thread_sensitive=True)(*args, **kwargs)


# V2: persistencia comercial explícita. API/render/emisión se conectan en P3/P4;
# los servicios v1 anteriores mantienen su contrato y no reinterpretan histórico.

def _commercial_data(partidas):
    salida, keys = [], set()
    for entrada in partidas:
        if len(salida) >= MAX_ITEMS:
            raise ValidationError(f"Máximo {MAX_ITEMS} partidas por revisión.")
        if not isinstance(entrada, PartidaComercialCapturada) or not isinstance(entrada.line_key, UUID):
            raise TypeError("Se requiere PartidaComercialCapturada con line_key UUID estable.")
        if entrada.line_key in keys:
            raise ValidationError("Identidad de partida duplicada.")
        keys.add(entrada.line_key)
        datos = asdict(entrada)
        for field in ("name", "code", "description", "provider_code", "provider_name", "external_id", "brand", "model", "mpn"):
            _text(datos[field], required=field == "name", limit=80 if field == "provider_code" else 255 if field == "provider_name" else 6000)
        for field in ("source_unit_label", "sale_unit_label"):
            _text(datos[field], required=True, limit=80)
        model = QuoteSnapshot(**datos)
        model.clean_fields(exclude=["revision", "position"])
        model.commercial_input()
        # Valores canónicos de campos, no str de Decimal sin normalizar.
        salida.append({field: getattr(model, field) for field in datos})
    if not salida:
        raise ValidationError("Una revisión comercial requiere al menos una partida.")
    return salida


def _commercial_calculate(parts, tipo, tax, discount):
    from .commercial import DescuentoGlobal, calcular_cotizacion

    return calcular_cotizacion((QuoteSnapshot(**p).commercial_input() for p in parts),
        tipo_cambio=tipo, tasa_iva=tax, descuento=DescuentoGlobal(discount))


def _commercial_fingerprint(header, parts, tipo, tax, discount, issue_date, days, based_on, context_id):
    payload = {"schema": 2, "calculation": 1, "header": header, "parts": parts,
        "fx": None if tipo is None else {"value": str(decimal_limit(tipo.valor, 18, 8)),
            "source": tipo.origen, "date": tipo.fecha.astimezone(dt_timezone.utc).isoformat(), "context": str(context_id)},
        "tax": str(tax), "discount": str(discount), "date": issue_date.isoformat(),
        "days": days, "based_on": based_on}
    return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _commercial_stored(revision):
    fields = PartidaComercialCapturada.__dataclass_fields__
    return [{f: getattr(s, f) for f in fields} for s in revision.snapshots.all()]


def _commercial_actor(quote, actor):
    from .permissions import require_commercial_permission

    current = require_commercial_permission(actor)
    authorize(quote, current)
    return current


def _commercial_adjustments(actor, parts, old_parts, *, tipo, context_id, tax, discount, previous=None):
    from .permissions import DISCOUNT, FX, MANUAL_SALE, MARGIN, TAX, require_commercial_permission

    previous_parts = {p["line_key"]: p for p in old_parts}
    original_fields = ("source_kind", "source_unit_price", "source_currency", "cost_tax_rate_included",
        "provider_code", "provider_name", "external_id", "name", "brand", "model", "mpn", "stock", "source_unit_label")
    for p in parts:
        old = previous_parts.get(p["line_key"])
        if old and old["source_kind"] == "API" and any(p[f] != old[f] for f in original_fields):
            raise RevisionConflict("Editar no recaptura originales API; usar una selección nueva explícita.")
        if p["source_kind"] == "MANUAL" or p["pricing_mode"] == "MANUAL":
            manual_fields = ("source_kind", "pricing_mode", "source_unit_price", "source_currency",
                "cost_tax_rate_included", "manual_unit_price", "manual_currency", "manual_tax_rate_included")
            if old is None or any(p[f] != old[f] for f in manual_fields):
                require_commercial_permission(actor, MANUAL_SALE)
        if p["margin_percent"] is not None and p["margin_percent"] != (
                old["margin_percent"] if old and old["margin_percent"] is not None else MARGEN_INICIAL):
            require_commercial_permission(actor, MARGIN)
        for field in ("cost_tax_rate_included", "manual_tax_rate_included"):
            expected = old[field] if old else None
            if p[field] != expected and p[field] is not None and (old is not None or p[field] != TASA_IVA_INICIAL):
                require_commercial_permission(actor, TAX)
    if tax != (previous.tax_rate if previous else TASA_IVA_INICIAL):
        require_commercial_permission(actor, TAX)
    if discount != (previous.discount_percent if previous else Decimal("0")):
        require_commercial_permission(actor, DISCOUNT)
    if tipo is not None and (not isinstance(context_id, UUID)):
        raise ValidationError("FX comercial requiere exchange_context_id UUID del servidor.")
    if tipo is None and context_id is not None:
        raise ValidationError("No hay identidad de contexto sin FX.")
    old_tipo = _tipo(previous) if previous else None
    if (tipo, context_id) != (old_tipo, previous.exchange_context_id if previous else None):
        require_commercial_permission(actor, FX)
        if previous and context_id is not None and context_id == previous.exchange_context_id:
            raise ValidationError("Un cambio de FX requiere una identidad de contexto nueva.")


def _commercial_write_parts(revision, parts, actor, old_snapshots=()):
    old = {s.line_key: s for s in old_snapshots}
    for position, datos in enumerate(parts, 1):
        prior = old.get(datos["line_key"])
        same_units = prior is not None and all(getattr(prior, f) == datos[f] for f in (
            "source_unit_label", "sale_unit_label", "conversion_factor"))
        QuoteSnapshot.objects.create(revision=revision, position=position, **datos,
            captured_at=prior.captured_at if prior else timezone.now(),
            unit_validated_by_id=prior.unit_validated_by_id if same_units else actor.pk,
            unit_validated_at=prior.unit_validated_at if same_units else timezone.now())


def _create_commercial(quote, parts, *, actor, company, conditions, tipo_cambio, exchange_context_id,
        client, tax_rate, discount_percent, issue_date, valid_days, request_id, based_on=None, commercial_source=None):
    actor = _commercial_actor(quote, actor)
    if not isinstance(request_id, UUID):
        raise TypeError("request_id debe ser UUID estable entre reintentos.")
    header = _header(quote, company, conditions, client)
    tax = decimal_limit(tax_rate, 5, 4, maximum=Decimal("1"))
    discount = decimal_limit(discount_percent, 5, 2, maximum=Decimal("100"))
    if type(issue_date) is not date or type(valid_days) is not int or not 1 <= valid_days <= 3650:
        raise ValidationError("Fecha/vigencia comercial inválidas.")
    _valid_until(issue_date, valid_days)
    _commercial_calculate(parts, tipo_cambio, tax, discount)
    fingerprint = _commercial_fingerprint(header, parts, tipo_cambio, tax, discount,
        issue_date, valid_days, based_on, exchange_context_id)
    previous = quote.revisions.filter(request_id=request_id).first()
    if previous:
        if previous.schema_version != 2 or previous.request_fingerprint != fingerprint:
            raise RevisionConflict("request_id ya pertenece a otra intención.")
        return previous
    _commercial_adjustments(actor, parts, _commercial_stored(commercial_source) if commercial_source else [],
        tipo=tipo_cambio, context_id=exchange_context_id, tax=tax, discount=discount, previous=commercial_source)
    revision = QuoteRevision.objects.create(quote=quote,
        number=(quote.revisions.aggregate(number=Max("number"))["number"] or 0) + 1,
        based_on_id=based_on, request_id=request_id, request_fingerprint=fingerprint,
        schema_version=2, calculation_version=1, state_fingerprint=fingerprint,
        discount_percent=discount, header=header, tax_rate=tax, issue_date=issue_date, valid_days=valid_days,
        exchange_rate=decimal_limit(tipo_cambio.valor, 18, 8) if tipo_cambio else None,
        exchange_rate_source=tipo_cambio.origen if tipo_cambio else "",
        exchange_rate_date=tipo_cambio.fecha if tipo_cambio else None, exchange_context_id=exchange_context_id)
    _commercial_write_parts(revision, parts, actor)
    return revision


def crear_borrador_comercial(quote_id, partidas, *, actor, company, conditions,
        tipo_cambio=None, exchange_context_id=None, client=None, tax_rate=TASA_IVA_INICIAL,
        discount_percent=Decimal("0"), issue_date=None, valid_days=None, request_id=None):
    """Crea v2 explícita. No cambia defaults/contrato de crear_borrador v1."""
    with transaction.atomic():
        quote = Quote.objects.select_for_update(of=("self",)).select_related("advisor").get(pk=quote_id)
        return _create_commercial(quote, _commercial_data(partidas), actor=actor, company=company,
            conditions=conditions, tipo_cambio=tipo_cambio, exchange_context_id=exchange_context_id,
            client=client if client is not None else {"business_name": quote.client_name, "rfc": quote.client_rfc, "email": quote.client_email},
            tax_rate=tax_rate, discount_percent=discount_percent,
            issue_date=quote.issue_date if issue_date is None else issue_date,
            valid_days=quote.valid_days if valid_days is None else valid_days,
            request_id=request_id if request_id is not None else uuid4())


def guardar_borrador_comercial(revision_id, partidas, *, actor, expected_version, company, conditions,
        client, tipo_cambio, exchange_context_id, tax_rate, discount_percent, issue_date, valid_days):
    with transaction.atomic():
        quote, revision = _locked(revision_id, actor)
        actor = _commercial_actor(quote, actor)
        if revision.schema_version != 2 or revision.status != QuoteRevision.Status.DRAFT or type(expected_version) is not int or expected_version != revision.edit_version:
            raise RevisionConflict("El borrador comercial cambió o no es editable.")
        if type(issue_date) is not date or type(valid_days) is not int or not 1 <= valid_days <= 3650:
            raise ValidationError("Fecha/vigencia comercial inválidas.")
        _valid_until(issue_date, valid_days)
        header = _header(quote, company, conditions, client)
        parts = _commercial_data(partidas)
        tax = decimal_limit(tax_rate, 5, 4, maximum=Decimal("1"))
        discount = decimal_limit(discount_percent, 5, 2, maximum=Decimal("100"))
        _commercial_calculate(parts, tipo_cambio, tax, discount)
        _commercial_adjustments(actor, parts, _commercial_stored(revision), tipo=tipo_cambio,
            context_id=exchange_context_id, tax=tax, discount=discount, previous=revision)
        old_snapshots = list(revision.snapshots.all())
        for snapshot in old_snapshots:
            snapshot.delete()
        revision.header, revision.tax_rate, revision.discount_percent = header, tax, discount
        revision.issue_date, revision.valid_days = issue_date, valid_days
        revision.exchange_context_id = exchange_context_id
        revision.exchange_rate = decimal_limit(tipo_cambio.valor, 18, 8) if tipo_cambio else None
        revision.exchange_rate_source = tipo_cambio.origen if tipo_cambio else ""
        revision.exchange_rate_date = tipo_cambio.fecha if tipo_cambio else None
        revision.state_fingerprint = _commercial_fingerprint(header, parts, tipo_cambio, tax, discount,
            issue_date, valid_days, revision.based_on_id, exchange_context_id)
        revision.edit_version += 1
        revision.save()
        _commercial_write_parts(revision, parts, actor, old_snapshots)
        return revision


def crear_revision_comercial(revision_id, partidas, *, actor, request_id, tax_rate,
        discount_percent, tipo_cambio, exchange_context_id):
    """Transición/clonado explícito con partidas revisadas; acepta origen DRAFT v1.

    No convierte una cantidad decimal ni asigna margen a datos del origen. El
    consumidor aporta las líneas comerciales completas que el usuario validó.
    """
    with transaction.atomic():
        quote, source = _locked(revision_id, actor)
        _commercial_actor(quote, actor)
        if source.schema_version == 2 and source.status != QuoteRevision.Status.ISSUED:
            raise RevisionConflict("Solo una emisión v2 origina otra revisión comercial; el borrador se edita.")
        return _create_commercial(quote, _commercial_data(partidas), actor=actor,
            company=source.header["company"], conditions=source.header["conditions"],
            client=source.header["client"], tipo_cambio=tipo_cambio, exchange_context_id=exchange_context_id,
            tax_rate=tax_rate, discount_percent=discount_percent, issue_date=timezone.localdate(),
            valid_days=source.valid_days, request_id=request_id, based_on=source.pk,
            commercial_source=source if source.schema_version == 2 else None)


def _current_commercial_state(revision):
    if revision.schema_version != 2 or revision.calculation_version != 1:
        raise ValidationError("Versión comercial no soportada.")
    parts = _commercial_stored(revision)
    fingerprint = _commercial_fingerprint(revision.header, parts, _tipo(revision), revision.tax_rate,
        revision.discount_percent, revision.issue_date, revision.valid_days, revision.based_on_id, revision.exchange_context_id)
    if fingerprint != revision.state_fingerprint:
        raise RevisionConflict("El estado comercial no coincide con su huella; revisar integridad.")
    return _commercial_calculate(parts, _tipo(revision), revision.tax_rate, revision.discount_percent)


def _warning_permission(code, scope, revision):
    from .permissions import DISCOUNT, MANUAL_SALE, MARGIN

    if code == "MARGEN_CERO":
        return MARGIN
    if (code == "VENTA_BAJO_COSTO" and not scope) or (code == "TOTAL_CERO" and revision.discount_percent > 0):
        return DISCOUNT
    return MANUAL_SALE


def confirmar_advertencias_comerciales(revision_id, warnings, *, actor, expected_version, state_fingerprint):
    """Avisos (código, referencia UUID o vacío), elegidos de los vigentes del motor."""
    from .permissions import require_commercial_permission

    with transaction.atomic():
        quote, revision = _locked(revision_id, actor)
        actor = _commercial_actor(quote, actor)
        if revision.status != QuoteRevision.Status.DRAFT or type(expected_version) is not int or expected_version != revision.edit_version or state_fingerprint != revision.state_fingerprint:
            raise RevisionConflict("Recarga antes de confirmar el estado comercial.")
        result = _current_commercial_state(revision)
        actual = {(str(w.codigo), w.referencia or "") for w in result.advertencias}
        records = []
        for index, warning in enumerate(warnings):
            if index >= MAX_ITEMS * 3 + 5:
                raise ValidationError("Demasiadas confirmaciones.")
            if not isinstance(warning, (tuple, list)) or len(warning) != 2 or not all(isinstance(v, str) for v in warning):
                raise ValidationError("Referencia de advertencia inválida.")
            code, scope = warning
            if (code, scope) not in actual:
                raise ValidationError("La advertencia no pertenece al estado actual.")
            require_commercial_permission(actor, _warning_permission(code, scope, revision))
            filters = dict(revision=revision, edit_version=revision.edit_version,
                state_fingerprint=revision.state_fingerprint, warning_code=code, scope_ref=scope, confirmed_by=actor)
            record = QuoteCommercialConfirmation.objects.filter(**filters).first()
            if record is None:
                record = QuoteCommercialConfirmation(**filters)
                record.save(_confirm=True)
            records.append(record)
        return tuple(records)


def validar_confirmaciones_comerciales(revision_id, *, actor, expected_version):
    """Comprueba P2; NO emite. P3 reutilizará esto en la transacción durable."""
    from .permissions import require_commercial_permission

    with transaction.atomic():
        quote, revision = _locked(revision_id, actor)
        _commercial_actor(quote, actor)
        if revision.status != QuoteRevision.Status.DRAFT or type(expected_version) is not int or expected_version != revision.edit_version:
            raise RevisionConflict("Recarga el borrador comercial.")
        result = _current_commercial_state(revision)
        records = list(revision.commercial_confirmations.filter(edit_version=revision.edit_version,
            state_fingerprint=revision.state_fingerprint).select_related("confirmed_by"))
        for warning in result.advertencias:
            code, scope = str(warning.codigo), warning.referencia or ""
            valid = False
            for record in records:
                if (record.warning_code, record.scope_ref) != (code, scope):
                    continue
                try:
                    current = require_commercial_permission(record.confirmed_by, _warning_permission(code, scope, revision))
                    authorize(quote, current)
                except PermissionDenied:
                    continue
                valid = True
                break
            if not valid:
                raise PermissionDenied("Falta confirmación comercial vigente y autorizada.")
        return result


async def acrear_borrador_comercial(*args, **kwargs):
    return await sync_to_async(crear_borrador_comercial, thread_sensitive=True)(*args, **kwargs)


async def aguardar_borrador_comercial(*args, **kwargs):
    return await sync_to_async(guardar_borrador_comercial, thread_sensitive=True)(*args, **kwargs)


async def acrear_revision_comercial(*args, **kwargs):
    return await sync_to_async(crear_revision_comercial, thread_sensitive=True)(*args, **kwargs)


async def aconfirmar_advertencias_comerciales(*args, **kwargs):
    return await sync_to_async(confirmar_advertencias_comerciales, thread_sensitive=True)(*args, **kwargs)
