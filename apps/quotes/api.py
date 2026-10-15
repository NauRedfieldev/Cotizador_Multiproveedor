"""API JSON de M7a (docs/28): búsqueda multi-proveedor para el cotizador.

Reutiliza el flujo del comparador (views.buscar) y responde el contrato de docs/27 §B2.
El dinero viaja como texto y nunca pasa por float. Las ofertas devueltas se guardan en la
sesión: al guardar una cotización (M8) el precio y la moneda salen de ahí, no del navegador.
"""

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from .views import buscar

# Ofertas vistas por el usuario, por "provider_code:external_id". Una búsqueda nueva pisa la
# entrada; se conservan las más recientes. La frescura del precio la decide M9 (docs/25 M8 §6).
CLAVE_OFERTAS = "m7a_ofertas"
MAX_OFERTAS_EN_SESION = 200


@require_GET
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
