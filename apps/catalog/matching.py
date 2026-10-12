"""M5b: decisión determinística de solo lectura, sin fuzzy ni persistencia.

El consumidor declara identificadores confiables tras verificar su significado.
No se infiere procedencia de provider_code, external_id ni raw. Las claves de
M5a localizan candidatos; diferencias originales distintas de mayúsculas o
espacios exteriores requieren revisión hasta aprobar reglas de equivalencia.
"""

from dataclasses import dataclass
from collections.abc import Iterable

from asgiref.sync import sync_to_async
from django.db.models import Q

from apps.providers.contracts import ProviderOffer

from .constants import EstadoMatch, MetodoMatch
from .models import ProductoCanonico
from .normalizer import normalizar, normalizar_clave


@dataclass(frozen=True, slots=True)
class IdentificadoresConfiables:
    """Declaración explícita; por defecto ninguna clave autoriza un match.

    sku_fabricante_confirmado se recibe aparte porque ProviderOffer no incluye
    ese campo. No pasar el SKU de Exel hasta confirmar su procedencia.
    """

    mpn_confirmado: bool = False
    modelo_confirmado: bool = False
    sku_fabricante_confirmado: str | None = None

    def __post_init__(self):
        if type(self.mpn_confirmado) is not bool or type(self.modelo_confirmado) is not bool:
            raise TypeError("La confirmación de identificadores debe ser bool.")
        if self.sku_fabricante_confirmado is not None and not isinstance(self.sku_fabricante_confirmado, str):
            raise TypeError("El SKU confirmado debe ser texto o None.")


@dataclass(frozen=True, slots=True)
class ResultadoMatch:
    canonico: ProductoCanonico | None
    estado: EstadoMatch
    metodo: MetodoMatch | None
    score: int | None
    motivo: str
    candidatos: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _Paso:
    metodo: MetodoMatch
    original: str
    normalizado: str
    valor: str
    clave: str


def _texto(valor: str | None) -> str:
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise TypeError("Los identificadores y la marca deben ser texto o None.")
    return valor


def _pasos(oferta: ProviderOffer, confianza: IdentificadoresConfiables) -> tuple[_Paso, ...]:
    pasos = []
    datos = (
        (confianza.mpn_confirmado, MetodoMatch.DETERMINISTICO_MPN, "mpn", "mpn_norm", oferta.mpn),
        (True, MetodoMatch.DETERMINISTICO_SKU, "sku_fabricante", "sku_norm", confianza.sku_fabricante_confirmado),
        (confianza.modelo_confirmado and bool(normalizar(_texto(oferta.brand))),
         MetodoMatch.DETERMINISTICO_MODELO, "modelo", "modelo_norm", oferta.model),
    )
    for confirmado, metodo, original, normalizado, valor in datos:
        if confirmado:
            texto = _texto(valor)
            clave = normalizar_clave(texto)
            if clave:
                pasos.append(_Paso(metodo, original, normalizado, texto, clave))
    return tuple(pasos)


def _firma_original(texto: str) -> str:
    # No elimina acentos, signos ni espacios internos; evita asumir equivalencias.
    return texto.strip().lower()


def _revision(motivo: str, metodo: MetodoMatch, candidatos: Iterable[ProductoCanonico]) -> ResultadoMatch:
    ids = tuple(sorted({c.pk for c in candidatos}))
    return ResultadoMatch(None, EstadoMatch.REVISION_MANUAL, metodo, None, motivo, ids)


def resolver_match(
    oferta: ProviderOffer,
    candidatos: Iterable[ProductoCanonico],
    *,
    confianza: IdentificadoresConfiables = IdentificadoresConfiables(),
) -> ResultadoMatch:
    """Resuelve en memoria. Pasar candidatos ya materializados para no usar ORM.

    No crea ni modifica registros. Ausencia de claves o coincidencia no impide
    un futuro fallback M5c. Score 100 solo para una asociación inequívoca.
    """
    pasos = _pasos(oferta, confianza)
    if not pasos:
        return ResultadoMatch(None, EstadoMatch.SIN_MATCH, None, None, "SIN_IDENTIFICADORES_CONFIABLES")
    marca = normalizar(_texto(oferta.brand))
    productos = list(candidatos)
    if any(c.pk is None for c in productos):
        raise ValueError("Los candidatos deben tener identificador interno.")
    productos = list({c.pk: c for c in productos}.values())
    if marca:
        productos = [c for c in productos if c.marca_norm == marca]
    coincidencias = [
        (paso, [c for c in productos if getattr(c, paso.normalizado) == paso.clave])
        for paso in pasos
    ]
    aciertos = [(paso, encontrados) for paso, encontrados in coincidencias if encontrados]
    if not aciertos:
        return ResultadoMatch(None, EstadoMatch.SIN_MATCH, None, None, "SIN_COINCIDENCIA_DETERMINISTICA")
    primero, encontrados = aciertos[0]
    if len(encontrados) > 1:
        return _revision("MULTIPLES_CANDIDATOS", primero.metodo, encontrados)
    elegido = encontrados[0]
    if not marca:
        return _revision("MARCA_AUSENTE", primero.metodo, encontrados)
    if _firma_original(oferta.brand) != _firma_original(elegido.marca):
        return _revision("COLISION_DE_MARCA", primero.metodo, encontrados)
    todos = [c for _, grupo in aciertos for c in grupo]
    if any(c.pk != elegido.pk for c in todos):
        return _revision("IDENTIFICADORES_CONTRADICTORIOS", primero.metodo, todos)
    # Una clave confiable que no encontró candidato también puede contradecir
    # al elegido. Un campo ausente en el canónico no constituye contradicción.
    for paso in pasos:
        valor_canonico = getattr(elegido, paso.normalizado)
        if valor_canonico and valor_canonico != paso.clave:
            return _revision("IDENTIFICADORES_CONTRADICTORIOS", primero.metodo, encontrados)
        if valor_canonico and _firma_original(paso.valor) != _firma_original(getattr(elegido, paso.original)):
            return _revision("COLISION_DE_NORMALIZACION", primero.metodo, encontrados)
    return ResultadoMatch(elegido, EstadoMatch.AUTO_MATCH, primero.metodo, 100,
                          "COINCIDENCIA_UNICA_VERIFICADA", (elegido.pk,))


def emparejar(
    oferta: ProviderOffer,
    *,
    confianza: IdentificadoresConfiables = IdentificadoresConfiables(),
) -> ResultadoMatch:
    """Busca candidatos indexados en BD y devuelve una decisión, sin escrituras."""
    pasos = _pasos(oferta, confianza)
    if not pasos:
        return resolver_match(oferta, (), confianza=confianza)
    filtro = Q()
    for paso in pasos:
        filtro |= Q(**{paso.normalizado: paso.clave})
    marca = normalizar(_texto(oferta.brand))
    candidatos = ProductoCanonico.objects.filter(filtro)
    if marca:
        candidatos = candidatos.filter(marca_norm=marca)
    return resolver_match(oferta, list(candidatos), confianza=confianza)


async def aemparejar(
    oferta: ProviderOffer,
    *,
    confianza: IdentificadoresConfiables = IdentificadoresConfiables(),
) -> ResultadoMatch:
    """Vía segura desde ASGI: no ejecuta ORM síncrono en el event loop."""
    return await sync_to_async(emparejar, thread_sensitive=True)(oferta, confianza=confianza)
