"""Servicios M8. M7 integra estas operaciones; M9 crea revisiones, nunca pisa emisiones.

Actores autenticados/activos: asesor propietario o staff. No se consulta raw ni
proveedores, ni se copian originales desde los campos legacy de dos decimales.
"""

import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

from asgiref.sync import sync_to_async
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.providers.contracts import ProviderOffer

from .comparison import OfertaComparada, TipoCambio
from .models import Quote, QuoteRevision, QuoteSnapshot
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
