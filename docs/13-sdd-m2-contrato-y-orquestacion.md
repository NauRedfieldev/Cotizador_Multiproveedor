# SDD M2 — Contrato y orquestación de proveedores

> **Fuentes:**
> - [AGENTS.md](../AGENTS.md), el documento rector.
> - El [SDD de M1](12-sdd-m1-providers-core.md): el núcleo original, que **se conserva sin cambios como retroalimentación**. Este SDD lo concreta y explica qué cambia (§6.4).
> - [02-modelo-de-datos.md](02-modelo-de-datos.md): M1a y la preparación de SYSCOM.
> - `docs/DER.md` y `apps/catalog/constants.py` de la rama del equipo `feature/der-modelos-catalog-quotes`, aún sin fusionar.
> - La documentación pública de SYSCOM, consultada el 2026-10-04.
> - Las decisiones del usuario del 2026-10-04, citadas como **[Decisión n]**:
>   1. Ahora solo se entrega el SDD.
>   2. `search_all` no guarda el crudo.
>   3. El núcleo gestiona el token OAuth2.
>   4. La documentación va en `docs/`.
>   5. La documentación no se borra.
>   6. Toda implementación termina documentando la funcionalidad.
>   7. M2 es contrato y orquestación; M3 es SYSCOM.
>
> Donde dice "(duda n)", el diseño usa la alternativa recomendada **de forma provisional**, hasta que se decida en §12.

## 1. Identificación

| Campo | Valor |
|---|---|
| ID | **M2** [Decisión 7] |
| Nombre | Contrato y orquestación de proveedores |
| Dueño | Por asignar (equipo) |
| Fase | Fundamentos: va antes de M3 (SYSCOM) |
| Rama | `feature/contrato-y-orquestacion` |
| Fecha | 2026-10-04 |
| Estado | **Implementado (2026-10-05).** Era un borrador hasta que se aprobó y se decidieron las dudas de §12 (todas A) [Decisión 1]. La documentación de la funcionalidad está en [14-contrato-y-orquestacion.md](14-contrato-y-orquestacion.md) |
| Antecesores | M1a (modelos de `apps/providers`, en `main`) · SDD de M1 §4–§11 |
| Sucesores | M3 (adaptador de SYSCOM) · emparejamiento de `catalog` (equipo) · vistas async |

## 2. Propósito y alcance

**Qué resuelve.** M2 da a `apps/providers` un contrato único para hablar con cualquier proveedor y una orquestación que consulta a todos los activos en paralelo. Devuelve sus ofertas normalizadas sin que un proveedor lento o caído afecte a los demás (AGENTS.md §Proyecto y §Arquitectura y Stack). Incluye:
- los contratos de datos (`ProductQuery`, `ProviderOffer`, `ProviderResult`) y los errores;
- el adaptador base y las funciones auxiliares que reutilizarán los adaptadores;
- la gestión del token OAuth2 sobre `ProviderToken` [Decisión 3];
- el registro de adaptadores;
- la orquestación `search_all`.

**Fuera de alcance (explícito):**
- Adaptadores reales. SYSCOM es M3 [Decisión 7] y no habrá ninguno de Cisco (SDD de M1 §7).
- Guardar el crudo en `RawProviderProduct` [Decisión 2]. El contrato lo deja preparado, porque la oferta lleva `external_id` y `raw`.
- Normalización y emparejamiento (`catalog`, del equipo), comparación de precios, moneda e IVA (`quotes`), PDF y vistas.
- La relación entre `catalog.Supplier` y `providers.Provider` (duda 7).
- Cambios en modelos, migraciones, `settings/` o dependencias.

## 3. Dependencias

Dirección permitida: `quotes → catalog → providers` (SDD de M1 §3).

- **M2 depende de:**
  - M1a: `Provider` (`code`, `active`, `base_url`, `timeout_ms`) y `ProviderToken` (`access_token`, `expires_at`, `obtained_at`), ver [02 §2](02-modelo-de-datos.md);
  - Django (ORM async);
  - `httpx` 0.28.1, que ya está en `requirements.txt`.
- **Consumen M2:**
  - **M3 (SYSCOM):** hereda de `ProviderAdapter`, implementa `fetch`, `parse_offer` y `solicitar_token`, y se registra con `@register`.
  - **`catalog` (equipo):** consume `ProviderOffer`. Su emparejamiento por MPN, SKU de fabricante y marca+modelo exactos (`catalog/constants.py`) usa `mpn`, `brand` y `model`.
  - **`quotes` (equipo):** usa `price` (Decimal) y `currency`. El IVA vive en `Quote.tax_rate` (`docs/DER.md`).
  - **Vistas async:** llaman a `search_all`.
- **Prohibido:** que `apps/providers` importe nada de `apps.catalog` o `apps.quotes`. Ya lo comprueba G4 de `test_reglas_repo.py`.

## 4. Contratos

**Congelado:** otros módulos dependen de él; cambiarlo exige revisión del equipo. **Interno:** puede cambiar sin avisar.

### 4.1 `apps/providers/contracts.py` — CONGELADO

```python
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

LIMITE_POR_DEFECTO = 60     # duda 8
LIMITE_MAXIMO = 1000        # SYSCOM admite limit 10–1000; cada adaptador lo traduce a su API

@dataclass(frozen=True, slots=True)
class ProductQuery:
    term: str = ""
    sku: str | None = None
    limit: int = LIMITE_POR_DEFECTO
    # __post_init__ (con object.__setattr__, porque es frozen):
    #   term se recorta; sku se recorta y "" pasa a None
    #   term vacío y sku None   -> ValueError("La consulta requiere 'term' o 'sku'.")
    #   limit fuera de 1..1000  -> ValueError("limit debe estar entre 1 y 1000.")

@dataclass(frozen=True, slots=True)
class ProviderOffer:
    provider_code: str
    external_id: str              # id del producto en el proveedor (mismo criterio que aupsert)
    name: str
    price: Decimal                # nunca float
    currency: str                 # ISO 4217 en mayúsculas: "USD", "MXN"
    brand: str | None = None      # duda 1: datos para el emparejamiento de catalog
    model: str | None = None
    mpn: str | None = None
    stock: int | None = None      # None = el proveedor no informa existencias
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    # __post_init__:
    #   external_id vacío o solo espacios -> ValueError("external_id no puede estar vacío.")
    #   price no es Decimal               -> TypeError("price debe ser Decimal, no <tipo>.")
    #   price negativo, NaN o infinito    -> ValueError("price debe ser un Decimal finito y >= 0.")
    #   currency no cumple ^[A-Z]{3}$     -> ValueError("currency debe ser un código ISO 4217 en mayúsculas.")
    #   stock negativo                    -> ValueError("stock no puede ser negativo.")

@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider_code: str
    offers: tuple[ProviderOffer, ...] = ()
    error: "ProviderError | None" = None
    elapsed_ms: int = 0
    discarded: int = 0            # duda 2: ofertas inválidas descartadas

    @property
    def ok(self) -> bool:
        return self.error is None
```

### 4.2 `apps/providers/errors.py` — CONGELADO

```python
class ProviderError(Exception):
    def __init__(self, provider_code: str, message: str) -> None: ...
    provider_code: str
    message: str
    # str(e) == f"[{provider_code}] {message}". Nunca incluye tokens, credenciales ni payloads.

class ProviderTimeout(ProviderError): ...           # se superó Provider.timeout_ms

class ProviderAuthError(ProviderError):             # rechazo de autenticación
    def __init__(self, provider_code: str, message: str, *, status: int | None = None) -> None: ...
    status: int | None    # 401: el núcleo renueva el token y reintenta una vez; 403: no se reintenta

class ProviderRateLimitError(ProviderError):        # HTTP 429 (duda 3)
    def __init__(self, provider_code: str, message: str, *, retry_after: int | None = None) -> None: ...
    retry_after: int | None   # segundos del header Retry-After, si es un entero

class ProviderResponseError(ProviderError): ...     # otros 4xx/5xx, JSON inválido, conexión, datos
class ProviderConfigError(ProviderError): ...       # sin adaptador, sin credenciales, adaptador incompleto
```

### 4.3 `apps/providers/adapters/base.py` — CONGELADO (la clase y `TokenNuevo`) / INTERNO (las funciones auxiliares, que los adaptadores deben usar)

```python
@dataclass(frozen=True, slots=True)
class TokenNuevo:
    access_token: str = field(repr=False)   # nunca aparece en repr ni en logs
    expires_in: int                         # segundos de vida (> 0)

class ProviderAdapter(ABC):
    code: ClassVar[str]                                   # igual a Provider.code
    required_credentials: ClassVar[tuple[str, ...]] = ()  # p. ej. ("CLIENT_ID", "CLIENT_SECRET")
    uses_token: ClassVar[bool] = False                    # True si la API usa un token OAuth2

    def __init__(self, provider: Provider, credentials: Mapping[str, str]) -> None:
        self.provider = provider
        self.credentials = credentials

    @abstractmethod
    async def fetch(self, query: ProductQuery,
                    client: httpx.AsyncClient) -> list[Mapping[str, Any]]:
        """Pide los productos a la API y devuelve los elementos crudos, sin convertir.
        Usa raise_for_status y parse_json. Si uses_token, obtiene el token con await self.token(client)."""

    @abstractmethod
    def parse_offer(self, item: Mapping[str, Any]) -> ProviderOffer:
        """Convierte un elemento crudo en oferta (provider_code = self.code, raw = item).
        Si el elemento es inválido lanza ProviderResponseError, ValueError, TypeError o KeyError,
        y el núcleo lo descarta (duda 2)."""

    async def solicitar_token(self, client: httpx.AsyncClient) -> TokenNuevo:
        """Solo si uses_token. Debe traducir el rechazo de credenciales del endpoint de tokens
        (400/401) a ProviderAuthError."""
        raise ProviderConfigError(self.code, "El adaptador no implementa solicitar_token.")

    async def token(self, client: httpx.AsyncClient) -> str:
        """Token vigente: se reutiliza el de ProviderToken o se renueva (tokens.obtener_token)."""
        return await obtener_token(self.provider, lambda: self.solicitar_token(client))

# Funciones auxiliares para los adaptadores:
def get_credentials(code: str, keys: tuple[str, ...]) -> dict[str, str]
    # Lee os.environ[f"PROVIDER_{CODE}_{KEY}"]. Los guiones del code pasan a guion bajo,
    # porque una variable de entorno no admite "-". Si falta alguna ->
    # ProviderConfigError(code, "Faltan credenciales: PROVIDER_SYSCOM_CLIENT_ID, …").
    # El mensaje lleva los nombres, nunca los valores.
def parse_json(code: str, response: httpx.Response) -> Any
    # json.loads(response.text, parse_float=Decimal); si falla ->
    # ProviderResponseError(code, "Respuesta no es JSON válido.")
def parse_price(code: str, value: object) -> Decimal
    # Acepta Decimal, int (no bool) y str numérica. Si recibe float, bool, None, NaN/Infinity
    # o un negativo -> ProviderResponseError(code, f"Precio inválido: {value!r}")
def raise_for_status(code: str, response: httpx.Response) -> None
    # 401/403 -> ProviderAuthError(code, f"Autenticación rechazada (HTTP {s}).", status=s)
    # 429     -> ProviderRateLimitError(code, "Límite de peticiones superado (HTTP 429).",
    #                                   retry_after=<Retry-After si es entero, si no None>)
    # >= 400  -> ProviderResponseError(code, f"HTTP {s} del proveedor.")
```

### 4.4 `apps/providers/tokens.py` — INTERNO

```python
MARGEN_RENOVACION = timedelta(minutes=5)          # duda 4

async def obtener_token(provider: Provider,
                        solicitar: Callable[[], Awaitable[TokenNuevo]]) -> str
    # 1. Si el ProviderToken del proveedor vence después de ahora + margen, devuelve su access_token.
    # 2. Si no, toma un asyncio.Lock por (bucle de eventos, proveedor) (duda 5) y vuelve a mirar,
    #    por si otra tarea ya lo renovó.
    # 3. Si sigue sin valer, llama a solicitar() y lo guarda con
    #    ProviderToken.objects.aupdate_or_create(provider=provider, defaults={
    #        "access_token": t.access_token,
    #        "expires_at": ahora + timedelta(seconds=t.expires_in),
    #        "obtained_at": ahora})
    # Si solicitar() falla, el error se propaga tal cual y no se guarda nada.

async def invalidar_token(provider: Provider) -> None
    # ProviderToken.objects.filter(provider=provider).aupdate(expires_at=ahora):
    # la fila se conserva; la siguiente llamada renueva el token.
```

### 4.5 `apps/providers/registry.py` — CONGELADO (`register`) / INTERNO (el resto)

```python
def register(cls: type[ProviderAdapter]) -> type[ProviderAdapter]
    # Decorador. Errores:
    #   sin code                         -> ValueError(f"El adaptador {cls.__name__} no define code.")
    #   code fuera de ^[a-z0-9_-]+$      -> ValueError(f"Código de adaptador inválido: {code!r}.")
    #   code ya registrado               -> ValueError(f"Adaptador duplicado para el proveedor '{code}'.")
def get_adapter_class(code: str) -> type[ProviderAdapter] | None
def registered_codes() -> list[str]              # ordenada
```

### 4.6 `apps/providers/service.py` — CONGELADO

```python
async def search_all(
    query: ProductQuery,
    *,
    transport: httpx.AsyncBaseTransport | None = None,   # solo para pruebas (httpx.MockTransport)
) -> list[ProviderResult]
    # Devuelve un ProviderResult por cada Provider con active=True, en orden de Provider.code.
    # Nunca lanza ProviderError: los errores van dentro de cada resultado.
    # Deja propagar asyncio.CancelledError.
    # Solo escribe en la BD para guardar o invalidar el token (tokens.py) [Decisión 2].
```

### 4.7 Modelos que usa M2 (M1a, sin cambios)

`Provider` y `ProviderToken`, tal como los describen [02 §2](02-modelo-de-datos.md) y el SDD de M1 §4.6. M2 no crea modelos ni migraciones.

## 5. Archivos

| Puede tocar | Crea | Prohibido tocar |
|---|---|---|
| `apps/providers/apps.py`: `ready()` importa `apps.providers.adapters` | `apps/providers/contracts.py`, `errors.py`, `tokens.py`, `registry.py`, `service.py` | `apps/providers/models.py` y `migrations/` (M2 no los necesita) |
| `apps/providers/tests/soporte.py`: utilidad para registrar adaptadores falsos | `apps/providers/adapters/__init__.py` (importará los adaptadores reales a partir de M3), `adapters/base.py` | `apps/catalog/**`, `apps/quotes/**` |
| `MEMORY.md`, y AGENTS.md según §11 T6 | `apps/providers/tests/test_contracts.py`, `test_helpers_adaptador.py`, `test_registry.py`, `test_tokens.py`, `test_service.py` | `settings/**`, `requirements*.txt` (AGENTS.md §Límites) |
| | `docs/14-contrato-y-orquestacion.md` (§11 T7) [Decisión 6] | Cualquier adaptador real (SYSCOM es M3) o de Cisco; `.env` |

Todos los nombres de archivo cumplen `^[a-z0-9_]+$` (G5 de `test_reglas_repo.py`).

## 6. Diseño

### 6.1 Componentes

```
                         ┌──────────────── apps/providers ────────────────┐
vista async ──query──►   │ service.search_all                              │
(o catalog)              │   ├─ registry ── adaptador por Provider.code    │
                         │   ├─ adapters/base ── fetch / parse_offer       │
                         │   │        └─ token() ── tokens ── ProviderToken (BD)
                         │   └─ contracts / errors                         │
                         └───────────────┬─────────────────────────────────┘
◄── list[ProviderResult] ────────────────┘        httpx.AsyncClient ──► APIs de los proveedores
```

### 6.2 Flujo de `search_all`

```
search_all(query)
 ├─ proveedores = [p async for p in Provider.objects.filter(active=True).order_by("code")]
 ├─ si no hay ninguno → return []                                (no se abre ninguna conexión)
 ├─ async with httpx.AsyncClient(transport=transport, timeout=None) as client:
 │     resultados = await asyncio.gather(*(_consultar(p, query, client) for p in proveedores),
 │                                       return_exceptions=True)
 │     (si queda alguna excepción en resultados, se convierte en ProviderResponseError,
 │      salvo CancelledError, que se relanza)
 └─ return resultados                                            (en el orden de proveedores)

_consultar(p, query, client) -> ProviderResult        (nunca lanza ProviderError)
   cls = get_adapter_class(p.code)            ── None → ProviderConfigError("No hay adaptador registrado.")
   creds = get_credentials(p.code, cls.required_credentials)       ── faltan → ProviderConfigError
   adaptador = cls(p, creds)
   items = await asyncio.wait_for(_traer(adaptador, query, client), timeout=p.timeout_ms / 1000)
   ofertas, descartadas = _convertir(adaptador, items)             (política de la duda 2)
   TimeoutError / httpx.TimeoutException → ProviderTimeout(f"Sin respuesta en {p.timeout_ms} ms.")
   ProviderError                         → se guarda en ProviderResult.error
   httpx.HTTPError                       → ProviderResponseError(f"Error de conexión: {tipo}.")
   Exception                             → ProviderResponseError(f"Error inesperado: {tipo}.") + logger.exception
   return ProviderResult(p.code, ofertas, error, elapsed_ms, descartadas)

_traer(adaptador, query, client)                       (todo dentro del mismo timeout)
   try:  return await adaptador.fetch(query, client)
   except ProviderAuthError(status=401) si adaptador.uses_token:
         await invalidar_token(adaptador.provider)
         return await adaptador.fetch(query, client)     ← un único reintento

_convertir(adaptador, items)
   para cada item: adaptador.parse_offer(item)
      ProviderResponseError / ValueError / TypeError / KeyError → descartada (logger.warning sin payload)
      oferta con provider_code distinto del adaptador           → descartada
```

El `httpx.AsyncClient` se crea **sin timeout propio** (`timeout=None`). Así manda el `wait_for` con `Provider.timeout_ms`; con el timeout por defecto de httpx (5 s) el corte llegaría antes que los 8 s configurados.

### 6.3 Gestión del token (OAuth2 `client_credentials`)

```
fetch() del adaptador ── await self.token(client) ── tokens.obtener_token(provider, solicitar)
   ProviderToken vigente (vence después de ahora + 5 min) ──► se reutiliza (0 peticiones)
   caducado o inexistente ──► Lock(bucle, proveedor) ──► se vuelve a mirar ──► solicitar_token()
        ──► aupdate_or_create(ProviderToken) ──► access_token
401 en fetch() ──► invalidar_token (expires_at = ahora; la fila se conserva) ──► un reintento
```

- **Por qué un Lock por (bucle, proveedor):** las pruebas con `async_to_sync` crean un bucle de eventos por llamada, y un `asyncio.Lock` no debe compartirse entre bucles.
- **Varios workers de uvicorn:** cada uno tiene su bucle. Si dos renuevan a la vez, gana la última escritura y los dos tokens son válidos (duda 5).
- **SYSCOM:** el token dura un año (/docs/autenticacion), así que la renovación es excepcional.

### 6.4 Relación con el SDD de M1: qué cambia y por qué

El SDD de M1 **no se modifica** [Decisión 5]. Este apartado registra la evolución:

| Tema | SDD de M1 | SDD de M2 | Por qué |
|---|---|---|---|
| Adaptador | `search(query, client)` devuelve ofertas | `fetch` devuelve elementos crudos y `parse_offer` convierte cada uno | El núcleo aplica la misma política a las ofertas inválidas (duda 2) y deja preparado el guardado del crudo |
| `ProviderOffer` | `sku`, `name`, `price`, `currency`, `stock`, `raw` | `external_id`, `name`, `brand`, `model`, `mpn`, `price`, `currency`, `stock`, `raw` | El emparejamiento del equipo (`catalog/constants.py`) y `aupsert` necesitan esos campos (duda 1) |
| `ProviderResult` | sin `discarded` | `discarded` | Que se vean las ofertas descartadas |
| Errores | 401/403 sin distinguir; 429 genérico | `ProviderAuthError.status`; `ProviderRateLimitError` | SYSCOM distingue 401 (token) de 403 (cuenta o scope) y manda `Retry-After` en el 429 (/docs/errores) |
| Tokens | Cada adaptador (§2) | El núcleo (`tokens.py` sobre `ProviderToken`) | [Decisión 3] |
| `ProductQuery` | `term`, `sku` | `+ limit` | SYSCOM pagina con `limit` (duda 8) |
| Timeout de httpx | Sin especificar | Cliente sin timeout propio; manda `wait_for` | El timeout por defecto de httpx (5 s) cortaría antes que `timeout_ms` (8 s) |
| Persistencia | No prevista | No por ahora | [Decisión 2] |
| Modelos | `Provider` por crear | `Provider` y `ProviderToken` ya existen (M1a) | — |

### 6.5 Decisiones

| # | Decisión | Justificación | Fuente |
|---|---|---|---|
| D1 | Todo en `apps/providers/`, un archivo por adaptador en `adapters/` | Sin app por proveedor y sin microservicios | AGENTS.md §Reglas de Dominio |
| D2 | `asyncio.gather(return_exceptions=True)` + `wait_for` por proveedor | Un proveedor lento o caído no tumba a los demás; sin `TaskGroup` | AGENTS.md §Arquitectura; SDD de M1 D2 |
| D3 | Un `httpx.AsyncClient` compartido por búsqueda, con `transport` inyectable | Un solo pool de conexiones; pruebas sin red | SDD de M1 D3 |
| D4 | Dinero siempre en `Decimal`; JSON con `parse_float=Decimal` | Prohibido float en dinero (G2) | SDD de M1 D4 |
| D5 | Errores como datos (`ProviderResult.error`) | El cotizador muestra los proveedores que sí respondieron | SDD de M1 D6; AGENTS.md §Proyecto |
| D6 | El núcleo gestiona el token sobre `ProviderToken` | Un solo mecanismo para M3 y los proveedores futuros | [Decisión 3]; SDD de M1 D5 |
| D7 | Sin persistencia en `search_all` | Pendiente de que SYSCOM confirme el uso ([02 §8.2](02-modelo-de-datos.md)) | [Decisión 2] |
| D8 | ORM async en todo el núcleo | Lo llamarán vistas `async def`; sin `ThreadPoolExecutor` | AGENTS.md §Arquitectura |

## 7. Reglas duras aplicables

1. Sin `float` en precios: ni en contratos, ni en parseo, ni en pruebas. G2 vigila que no haya llamadas a `float(` en `apps/providers`.
2. Concurrencia solo con `async def` + `asyncio.gather`; sin `TaskGroup` ni `ThreadPoolExecutor` (G1).
3. ASGI, nunca WSGI. Nada bloqueante: ni `requests` ni `httpx.Client` síncrono (AGENTS.md §Arquitectura).
4. ORM async en código async (`async for`, `aget`, `aupdate`, `aupdate_or_create`).
5. Ningún adaptador de Cisco (G3).
6. Todo dentro de `apps/providers/`; sin importar `catalog`/`quotes` (G4; AGENTS.md §Reglas de Dominio).
7. Sin dependencias nuevas (AGENTS.md §Límites).
8. Tokens y credenciales nunca en logs, mensajes de error, `repr` ni el admin (SDD de M1 D5).
9. Nombres de archivo en snake_case (G5).
10. Hacer solo lo especificado aquí (AGENTS.md §Forma de trabajar).

## 8. Manejo de errores y casos límite

| Situación | Comportamiento esperado |
|---|---|
| `ProductQuery(term="  ")` sin `sku` | `ValueError("La consulta requiere 'term' o 'sku'.")`; no se llama a nadie |
| `limit` fuera de 1..1000 | `ValueError("limit debe estar entre 1 y 1000.")` |
| Ningún proveedor activo | `[]`, sin abrir conexiones |
| Proveedor activo sin adaptador registrado | `ProviderConfigError(code, "No hay adaptador registrado.")` |
| Faltan variables `PROVIDER_<CODE>_*` | `ProviderConfigError(code, "Faltan credenciales: …")` con los nombres; sin petición HTTP |
| `uses_token=True` sin implementar `solicitar_token` | `ProviderConfigError(code, "El adaptador no implementa solicitar_token.")` |
| Adaptador registrado sin fila `Provider`, o con `active=False` | No se consulta y no aparece en el resultado |
| Se supera `timeout_ms`, o httpx lanza `TimeoutException` | `ProviderTimeout(code, "Sin respuesta en 8000 ms.")`; los demás siguen |
| 401 con token | Se invalida el token, se renueva y se reintenta una vez; si vuelve a dar 401, `ProviderAuthError(status=401)` |
| 401 sin token, o 403 | `ProviderAuthError(status=…)`, sin reintento |
| 429 | `ProviderRateLimitError(retry_after=…)`, sin reintento (duda 3) |
| 404, 5xx u otro 4xx | `ProviderResponseError(code, "HTTP 503 del proveedor.")` |
| Cuerpo que no es JSON | `ProviderResponseError(code, "Respuesta no es JSON válido.")` |
| `httpx.ConnectError` u otra `httpx.HTTPError` | `ProviderResponseError(code, "Error de conexión: ConnectError.")` |
| Excepción inesperada en el adaptador | `ProviderResponseError(code, "Error inesperado: RuntimeError.")` + `logger.exception`; no afecta al resto |
| Oferta inválida (precio, moneda, `external_id` vacío, falta una clave) | Se descarta, `discarded += 1` y aviso en el log sin payload (duda 2) |
| Oferta con un `provider_code` distinto del adaptador | Se descarta y se cuenta |
| El endpoint de tokens rechaza las credenciales | `ProviderAuthError` (lo traduce `solicitar_token`); no se guarda token |
| Dos búsquedas renuevan el token a la vez en el mismo proceso | Una sola llamada a `solicitar_token` (Lock) |
| Se cancela la petición entrante | `asyncio.CancelledError` se propaga; no se convierte en resultado |
| Dos adaptadores con el mismo `code` | `ValueError` al importar, es decir, al arrancar (falla `manage.py check`) |
| `code` con guion, p. ej. `ct-online` | Las variables de entorno se llaman `PROVIDER_CT_ONLINE_*` |
| Todos los proveedores fallan | Lista de resultados, todos con `ok == False`; qué ve el usuario lo deciden las vistas |

## 9. Plan de pruebas

Se ejecutan con el runner de Django (AGENTS.md §Pruebas): `TestCase` con métodos `async def`, `httpx.MockTransport` sin red, `RelojControlado` de `tests/soporte.py` para el tiempo, y adaptadores falsos registrados con una utilidad nueva de `soporte.py`. Etiquetas: `unit` para las pruebas sin BD y `django_db` para las demás.

**Contratos (`test_contracts.py`)**
1. `ProductQuery(term=" laptop ", sku=" ")` → `term == "laptop"`, `sku is None`, `limit == 60`.
2. `ProductQuery()` → `ValueError` con el mensaje de §8.
3. `ProductQuery(term="x", limit=0)` y `limit=1001` → `ValueError`.
4. `ProviderOffer` válida → se crea. `raw` no participa en `==` y no aparece en `repr`.
5. `price=10.5` (float) → `TypeError`.
6. `price` negativo, `Decimal("NaN")` o `Decimal("Infinity")` → `ValueError`.
7. `currency="usd"` y `"PESOS"` → `ValueError`.
8. `external_id=""` y `"  "` → `ValueError`.
9. `stock=-1` → `ValueError`.
10. `ProviderResult("x")` → `ok` es True y `discarded` es 0; con `error` → `ok` es False.

**Errores y auxiliares (`test_helpers_adaptador.py`)**

11. `str(ProviderTimeout("x", "m")) == "[x] m"`. `ProviderAuthError(..., status=401).status == 401`. `ProviderRateLimitError(..., retry_after=30).retry_after == 30`.
12. `get_credentials("demo", ("API_KEY",))` → devuelve el valor si existe. Si falta → `ProviderConfigError` con el nombre de la variable y sin su valor. Con `code="ct-online"` lee `PROVIDER_CT_ONLINE_API_KEY`.
13. `parse_json` sobre `{"price": 19.99}` → `Decimal("19.99")`. Sobre `"<html>"` → `ProviderResponseError`.
14. `parse_price` acepta `Decimal("1.10")`, `5` y `"1.10"`, y rechaza `1.1`, `True`, `None`, `"abc"`, `"NaN"` y `"-3"`.
15. `raise_for_status`:
    - 401 y 403 → `ProviderAuthError` con su `status`;
    - 429 con `Retry-After: 30` → `retry_after == 30`;
    - 429 con `Retry-After` en formato fecha → `None`;
    - 404 y 500 → `ProviderResponseError`;
    - 200 → no lanza.
16. `repr(TokenNuevo("secreto", 60))` no contiene `"secreto"`.

**Registro (`test_registry.py`)**

17. `@register` con `code="demo"` → `get_adapter_class("demo")` lo devuelve y `registered_codes()` sale ordenado.
18. `code` repetido → `ValueError` de duplicado.
19. Sin `code`, o con `code="Demo X"` → `ValueError`.
20. `get_adapter_class("inexistente")` → `None`.

**Tokens (`test_tokens.py`)**, con `RelojControlado`

21. Sin `ProviderToken` → llama una vez a `solicitar` y guarda `expires_at = ahora + expires_in`.
22. Token que vence en más de 5 minutos → se reutiliza y no llama a `solicitar`.
23. Token que vence en 4 minutos → se renueva.
24. `invalidar_token` → la fila sigue existiendo y la siguiente llamada pide un token nuevo.
25. Dos `obtener_token` simultáneos (`gather`) → `solicitar` se llama una sola vez.
26. `solicitar` lanza `ProviderAuthError` → el error se propaga y no se guarda nada.

**Servicio (`test_service.py`)**, con filas `Provider` y `MockTransport`

27. Dos proveedores activos con adaptadores falsos → 2 resultados `ok`, ordenados por `code` y con precios Decimal.
28. Un adaptador que duerme 1 s con `timeout_ms=50` → `ProviderTimeout`. El otro responde bien y el total tarda menos de 1 s (prueba de que van en paralelo).
29. El transport lanza `httpx.ReadTimeout` → `ProviderTimeout`.
30. Un 401 en la primera llamada con `uses_token` → se renueva y se reintenta, y el resultado es `ok`. El transport ve 2 peticiones de datos y 2 de token.
31. Dos 401 seguidos → `ProviderAuthError(status=401)`.
32. 403 → `ProviderAuthError(status=403)` con una sola petición.
33. 429 con `Retry-After: 30` → `ProviderRateLimitError(retry_after=30)`, sin reintento.
34. 503 → `ProviderResponseError("HTTP 503 del proveedor.")`.
35. 3 elementos, uno con precio inválido → 2 ofertas, `discarded == 1` y `ok`.
36. Oferta con un `provider_code` ajeno → se descarta.
37. `httpx.ConnectError` → `ProviderResponseError("Error de conexión: ConnectError.")`.
38. Un adaptador lanza `RuntimeError` → `ProviderResponseError("Error inesperado: RuntimeError.")` y se registra con `logger.exception`; el otro proveedor sigue `ok`.
39. Proveedor activo sin adaptador → `ProviderConfigError`. Proveedor inactivo → no aparece. Ninguno activo → `[]` y el transport no recibe nada.
40. Faltan credenciales → `ProviderConfigError` y el transport no recibe nada.
41. Se cancela la tarea que ejecuta `search_all` → `CancelledError`.
42. En ningún log capturado (`assertLogs`) aparecen el token centinela ni una credencial centinela.

Además siguen pasando las 103 pruebas anteriores, incluidas G1–G9.

## 10. Criterio de aceptación

| Comando o comprobación | Resultado esperado |
|---|---|
| `python manage.py check` | `System check identified no issues (0 silenced).` |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` (M2 no toca modelos) |
| `python manage.py test apps.providers` | Las 103 anteriores más las de §9, en `OK` |
| `coverage run --branch … manage.py test apps.providers` + `coverage report -m` | 100 % de líneas y ramas en `contracts`, `errors`, `tokens`, `registry`, `service` y `adapters/base` |
| Mutaciones, como en la QA de M1a | Cada regla de §8 tiene al menos un test que falla si se rompe |
| `docs/14-contrato-y-orquestacion.md` | Existe y cubre lo pedido en §11 T7 |

## 11. Tareas de implementación

| # | Tarea | Depende de |
|---|---|---|
| T1 | `errors.py` y `contracts.py` + pruebas 1–11 | — |
| T2 | `adapters/base.py` (`TokenNuevo`, `ProviderAdapter` y funciones auxiliares) + pruebas 12–16 | T1 |
| T3 | `registry.py`, `adapters/__init__.py` y `ProvidersConfig.ready()` + pruebas 17–20 | T2 |
| T4 | `tokens.py` + pruebas 21–26 | T2 |
| T5 | `service.py` (`search_all`, `_consultar`, `_traer`, `_convertir`) y la utilidad de adaptadores falsos en `soporte.py` + pruebas 27–42 | T3, T4 |
| T6 | Criterio de aceptación (§10), comprobación por mutaciones, y actualizar MEMORY.md y AGENTS.md (§12, cambios de AGENTS.md al implementar) | T5 |
| T7 | **Documentar la funcionalidad** en `docs/14-contrato-y-orquestacion.md` [Decisión 6] (ver detalle abajo) | T6 |

Detalle de T7:
- **Cómo funciona:** el flujo de una búsqueda, el token, los errores, los timeouts y las ofertas descartadas.
- **Cómo se conecta con los demás componentes:**
  - con M1a (`Provider`, `ProviderToken` y, más adelante, `aupsert`);
  - con M3 (cómo se escribe y registra un adaptador, con SYSCOM de ejemplo);
  - con `catalog` (qué campos de la oferta usa el emparejamiento);
  - con `quotes` (precio Decimal, moneda e IVA);
  - con las vistas async.
- **Cómo se prueba y cómo se añade un proveedor nuevo.**

T1 es la base. T3 y T4 se pueden hacer en paralelo.

## 12. Riesgos y preguntas abiertas

### Decisiones tomadas (2026-10-05)

El usuario eligió **la alternativa A en todas las dudas**. La 7 quedó al principio para el equipo, porque M2 no la implementa. El 2026-10-05 el usuario la decidió también: **alternativa A, aplicada en `catalog`** (`Supplier.provider`, migración `catalog/0003_supplier_provider`, pruebas en `apps/catalog/tests/test_supplier_provider.py`). Sigue abierto, para el equipo, si `Supplier` debe guardar solo distribuidores o también marcas, como sugiere su definición. Las dudas se conservan abajo tal como se plantearon.

Por la duda 10 (A), los nombres en español de §4 y §6 se implementan en inglés. Los mensajes de error, las pruebas y la documentación siguen en español.

| En este SDD | En el código |
|---|---|
| `TokenNuevo` | `NewToken` |
| `solicitar_token` (y su mensaje "no implementa solicitar_token") | `request_token` ("El adaptador no implementa request_token.") |
| `obtener_token` / `invalidar_token` | `get_token` / `invalidate_token` |
| `MARGEN_RENOVACION` | `RENEWAL_MARGIN` |
| `LIMITE_POR_DEFECTO` / `LIMITE_MAXIMO` | `DEFAULT_LIMIT` / `MAX_LIMIT` |
| `_consultar` / `_traer` / `_convertir` | `_query_provider` / `_fetch_with_auth_retry` / `_parse_offers` |

### Dudas que hay que decidir antes de implementar

Cada una lleva alternativas alineadas con AGENTS.md; la recomendada está en **negrita**.

1. **Campos de `ProviderOffer`** (contrato congelado que consume `catalog`).
   - **A. Campos para el emparejamiento:** `external_id`, `name`, `brand`, `model`, `mpn` (opcionales), `price`, `currency`, `stock` y `raw`. Cada adaptador normaliza, así que `catalog` no depende del formato de cada API. Respeta la dirección `quotes → catalog → providers` y el monolito modular.
   - B. El mínimo del SDD de M1 (`sku`, `name`, `price`, `currency`, `stock`, `raw`). Es más simple, pero `catalog` tendría que leer el `raw` de cada proveedor, lo que lo acopla a cada API.
   - C. `atributos: dict` libre. Es flexible, pero se pierde el contrato tipado y los errores aparecen en ejecución.
   - Conviene confirmarlo con quien lleva `catalog`.
2. **Oferta inválida** (P10 del SDD de M1).
   - **A. Descartarla, contarla en `discarded` y dejar un aviso en el log.** El cotizador sigue mostrando los precios válidos (AGENTS.md §Proyecto).
   - B. Marcar todo el proveedor como error. Detecta antes un cambio de formato, pero una sola oferta rota deja al proveedor sin precios.
   - C. Descartar hasta un umbral (por ejemplo el 50 %) y, si se supera, marcar error. Es más equilibrado, pero añade lógica.
3. **HTTP 429.**
   - **A. `ProviderRateLimitError` sin reintento.** Una búsqueda en vivo no espera, y el usuario ve el resto de proveedores.
   - B. Reintentar una vez si el `Retry-After` cabe en el tiempo que queda. Da más resultados a cambio de más latencia.
   - C. Tratarlo como un error HTTP más. Es menos código, pero se pierde el `Retry-After`.
4. **Margen de renovación del token.**
   - **A. Fijo de 5 minutos.** Es simple, y con los tokens de un año de SYSCOM basta.
   - B. Proporcional, el 10 % de la vida del token. Va mejor con tokens cortos de otros proveedores.
   - C. Sin margen, confiando en el reintento por 401. Hace menos peticiones, pero la primera búsqueda tras caducar paga un fallo.
5. **Renovación del token desde varios procesos.**
   - **A. Un `asyncio.Lock` por (bucle, proveedor); entre procesos gana la última escritura.** No añade dependencias y, con tokens de un año, la coincidencia es rara.
   - B. Un bloqueo en PostgreSQL (advisory lock o `select_for_update`). Evita duplicados entre workers, pero retiene una transacción mientras se espera a la API externa.
   - C. Sin bloqueo. Puede pedir tokens duplicados, y el endpoint de tokens de SYSCOM tiene límites estrictos.
6. **Timeout global** (P5 del SDD de M1).
   - **A. Solo el de cada proveedor.** El total es el mayor de ellos, y ya se ajusta desde el admin.
   - B. Un tope global fijo en el código. Protege a la vista, pero corta proveedores lentos aunque su timeout sea mayor.
   - C. Un tope global en `settings`. Exige tocar `settings/`, que está fuera del alcance.
7. **`catalog.Supplier` frente a `providers.Provider`.** No se implementa en M2, pero hay que decidirlo antes del emparejamiento.
   - **A. `Supplier` con una FK opcional a `Provider`.** Respeta la dirección `catalog → providers`, y un supplier puede no tener API.
   - B. Unificar en `Provider` y retirar `Supplier`. Queda un solo concepto, pero cambia el modelo del equipo.
   - C. Mantenerlos separados y relacionarlos por `code`. No cambia nada, pero hay dos códigos que mantener sincronizados.
   - Lo decide el equipo.
8. **Límite de ofertas por proveedor.**
   - **A. `ProductQuery.limit`, por defecto 60, que cada adaptador traduce a su API.** El comportamiento es uniforme.
   - B. Que cada adaptador decida. Es más simple, pero cada proveedor se comporta distinto.
   - C. Que el núcleo recorte a N ofertas después de recibirlas. Desperdicia lo descargado.
9. **Prohibiciones que hoy solo están en las plantillas** (sin float, sin `TaskGroup`, sin Jinja2, sin Cisco, ORM async).
   - **A. Llevarlas a AGENTS.md §Reglas de Dominio.** Así las conoce cualquier agente, y ya hay tests (G1, G2) que las comprueban.
   - B. Dejarlas solo en las plantillas.
10. **Idioma de los nombres del contrato.** Hoy mezcla nombres en inglés (`fetch`, `parse_offer`, `search_all`) con nombres en español (`solicitar_token`, `obtener_token`, `TokenNuevo`).
    - **A. Todo en inglés en el código de producción** (`request_token`, `get_token`, `invalidate_token`, `NewToken`), como `models.py` y el SDD de M1. Las pruebas y la documentación siguen en español.
    - B. Mantener la mezcla tal como está en este SDD. No cambia nada respecto al plan aprobado.
    - C. Todo en español. Coincide con el código nuevo del equipo (`normalizar`, `EstadoMatch`), pero choca con `models.py`.

### Riesgos

| # | Riesgo | Efecto en M2 | Quién decide |
|---|---|---|---|
| R1 | El acuerdo de uso de SYSCOM prohíbe "motores de comparación de precios masivos" ([02 §8.2](02-modelo-de-datos.md)) | Ninguno: M2 no depende de ningún proveedor. Bloquea M3 | CCONOR con SYSCOM |
| R2 | `catalog.Supplier` y `providers.Provider` modelan lo mismo (duda 7) | Ninguno ahora; afecta al emparejamiento. **Mitigado el 2026-10-05** con la alternativa A (`Supplier.provider`); queda abierta la pregunta marca/distribuidor | Equipo |
| R3 | `RawProviderProduct` queda sin uso hasta que se decida la persistencia [Decisión 2] | Ninguno | Al implementar M3 |
| R4 | Moneda e IVA siguen abiertos (P4 del SDD de M1); `quotes` usa `tax_rate` 0.16 | M2 solo transporta `currency` | Negocio |

## 13. Prompt de implementación

```text
Implementa M2 (contrato y orquestación) del Cotizador CCONOR siguiendo
EXACTAMENTE docs/13-sdd-m2-contrato-y-orquestacion.md. Lee primero AGENTS.md y MEMORY.md.
Antes de empezar, comprueba en §12 qué alternativa se eligió en cada duda y aplícala.

Alcance: apps/providers/ (sin tocar models.py ni migrations/), MEMORY.md, AGENTS.md
(solo lo indicado en T6) y docs/14-contrato-y-orquestacion.md. No toques apps/catalog,
apps/quotes, settings/ ni requirements*.txt. No crees adaptadores reales (SYSCOM es M3) ni de Cisco.

Haz las tareas T1→T7 de la §11 en orden, con TDD: primero la prueba de la §9 y comprueba
que falla, después el código. No hagas commits ni push salvo que se pidan.
- Copia literalmente las firmas, los nombres y los mensajes de la §4 y la §8: son contratos.
- Dinero siempre en Decimal; JSON con json.loads(..., parse_float=Decimal).
- Concurrencia solo con asyncio.gather(return_exceptions=True) + asyncio.wait_for;
  sin TaskGroup ni ThreadPoolExecutor. ORM async.
- httpx.AsyncClient con timeout=None (manda wait_for). Nunca registres tokens ni credenciales.
- Pruebas con el runner de Django, httpx.MockTransport y tests/soporte.py (AGENTS.md §Pruebas).

Al terminar: ejecuta el criterio de la §10 y pega su salida, actualiza MEMORY.md y
escribe docs/14-contrato-y-orquestacion.md (T7). Si algo del SDD no se puede cumplir,
detente y dilo; no lo resuelvas por tu cuenta.
```
