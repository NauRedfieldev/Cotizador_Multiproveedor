"""M5b determinístico y M5c opcional de sugerencias, sin persistencia.

El consumidor declara identificadores confiables tras verificar su significado.
No se infiere procedencia de provider_code, external_id ni raw. Las claves de
M5a localizan candidatos; diferencias originales distintas de mayúsculas o
espacios exteriores requieren revisión hasta aprobar reglas de equivalencia.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.db.models import Q
from rapidfuzz import fuzz

from apps.providers.contracts import ProviderOffer

from .constants import EstadoMatch, MetodoMatch, UMBRAL_AUTO_MATCH, UMBRAL_REVISION_MANUAL
from .models import Category, ProductoCanonico
from .normalizer import normalizar, normalizar_clave
from .specs import compatibles, specs
from .synonyms import expandir_con_diccionario, obtener_sinonimos


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


def _sin_fallback(motivo: str, score: int | None = None) -> ResultadoMatch:
    return ResultadoMatch(None, EstadoMatch.SIN_MATCH, MetodoMatch.FUZZY, score, motivo)


def _contexto_fallback(oferta: ProviderOffer, categoria_id: int | None) -> ResultadoMatch | None:
    if not normalizar(_texto(oferta.brand)):
        return _sin_fallback("FALLBACK_SIN_MARCA")
    if categoria_id is None:
        return _sin_fallback("FALLBACK_SIN_CATEGORIA")
    if type(categoria_id) is not int or categoria_id <= 0:
        return _sin_fallback("FALLBACK_CATEGORIA_INVALIDA")
    if not re.search(r"[A-Z0-9]", normalizar(oferta.name)):
        return _sin_fallback("FALLBACK_TEXTO_SIN_INFORMACION")
    return None


def _resolver_fallback(
    oferta: ProviderOffer,
    candidatos: list[ProductoCanonico],
    *,
    categoria_id: int,
    confianza: IdentificadoresConfiables,
    sinonimos: Mapping[str, str],
) -> ResultadoMatch:
    texto = normalizar(oferta.name)
    texto_expandido = expandir_con_diccionario(texto, sinonimos)
    if not re.search(r"[A-Z0-9]", texto_expandido):
        return _sin_fallback("FALLBACK_TEXTO_SIN_INFORMACION")
    atributos = specs(texto)
    pasos = _pasos(oferta, confianza)
    puntuados = []
    marca = normalizar(_texto(oferta.brand))
    bloqueados = [c for c in candidatos if c.categoria_id == categoria_id
                  and c.marca_norm == marca]
    if not bloqueados:
        return _sin_fallback("FALLBACK_SIN_CANDIDATOS")
    for candidato in bloqueados:
        # No promover colisiones de marca ni identificadores conocidos a fuzzy.
        if _firma_original(candidato.marca) != _firma_original(oferta.brand):
            continue
        if any(getattr(candidato, paso.normalizado) and (
            getattr(candidato, paso.normalizado) != paso.clave
            or _firma_original(getattr(candidato, paso.original)) != _firma_original(paso.valor)
        ) for paso in pasos):
            continue
        descripcion = normalizar(candidato.descripcion)
        expandida = expandir_con_diccionario(descripcion, sinonimos)
        if not re.search(r"[A-Z0-9]", expandida):
            continue
        score = fuzz.token_set_ratio(texto_expandido, expandida)
        # Revisar cada candidato, no solo el de mayor score. No usar los textos
        # expandidos para guardas: un sinónimo no debe borrar POE+ o una licencia.
        if compatibles(atributos, specs(descripcion)):
            puntuados.append((score, candidato.pk))
    if not puntuados:
        return _sin_fallback("FALLBACK_SIN_CANDIDATOS_COMPATIBLES")
    mejor = max(score for score, _ in puntuados)
    # Comparar el float original; int() solo trunca la puntuación mostrada.
    sugerencias = [(score, pk) for score, pk in puntuados if score >= UMBRAL_REVISION_MANUAL]
    if not sugerencias:
        return _sin_fallback("FUZZY_BAJO_UMBRAL", int(mejor))
    if sum(score == mejor for score, _ in sugerencias) > 1:
        motivo = "FUZZY_EMPATE"
    elif mejor >= UMBRAL_AUTO_MATCH:
        motivo = "FUZZY_UMBRAL_ALTO_SIN_CALIBRAR"
    else:
        motivo = "SUGERENCIAS_FUZZY"
    return ResultadoMatch(None, EstadoMatch.REVISION_MANUAL, MetodoMatch.FUZZY,
                          int(mejor), motivo, tuple(sorted(pk for _, pk in sugerencias)))


def resolver_con_fallback(
    oferta: ProviderOffer,
    candidatos: Iterable[ProductoCanonico],
    *,
    categoria_id: int | None,
    sinonimos: Mapping[str, str],
    confianza: IdentificadoresConfiables = IdentificadoresConfiables(),
) -> ResultadoMatch:
    """Flujo en memoria; el consumidor valida categoría y materializa candidatos.

    sinonimos es una instantánea de BD o un fixture. M5b ve todos los candidatos;
    marca/categoría solo restringen el fallback, nunca ocultando ambigüedades M5b.
    """
    productos = list(candidatos)
    resultado = resolver_match(oferta, productos, confianza=confianza)
    if resultado.estado != EstadoMatch.SIN_MATCH:
        return resultado
    if any(c.pk is None for c in productos):
        raise ValueError("Los candidatos deben tener identificador interno.")
    productos = list({c.pk: c for c in productos}.values())
    bloqueo = _contexto_fallback(oferta, categoria_id)
    if bloqueo is not None:
        return bloqueo
    return _resolver_fallback(oferta, productos, categoria_id=categoria_id,
                             confianza=confianza, sinonimos=sinonimos)


def emparejar_con_fallback(
    oferta: ProviderOffer,
    *,
    categoria_id: int | None,
    confianza: IdentificadoresConfiables = IdentificadoresConfiables(),
) -> ResultadoMatch:
    """M5b primero, luego sugerencias M5c acotadas; sin escrituras ni red."""
    resultado = emparejar(oferta, confianza=confianza)
    if resultado.estado != EstadoMatch.SIN_MATCH:
        return resultado
    bloqueo = _contexto_fallback(oferta, categoria_id)
    if bloqueo is not None:
        return bloqueo
    if not Category.objects.filter(pk=categoria_id, is_active=True).exists():
        return _sin_fallback("FALLBACK_CATEGORIA_INVALIDA")
    candidatos = list(ProductoCanonico.objects.filter(
        marca_norm=normalizar(_texto(oferta.brand)), categoria_id=categoria_id,
    ))
    if not candidatos:
        return _sin_fallback("FALLBACK_SIN_CANDIDATOS")
    return _resolver_fallback(oferta, candidatos, categoria_id=categoria_id,
                             confianza=confianza, sinonimos=obtener_sinonimos())


async def aemparejar_con_fallback(
    oferta: ProviderOffer,
    *,
    categoria_id: int | None,
    confianza: IdentificadoresConfiables = IdentificadoresConfiables(),
) -> ResultadoMatch:
    """ORM, sinónimos y puntuación fuera del event loop, sin cambiar M5b."""
    return await sync_to_async(emparejar_con_fallback, thread_sensitive=True)(
        oferta, categoria_id=categoria_id, confianza=confianza,
    )
