# 15 · Guía de traspaso: de M2 (contrato y orquestación) a M3 (SYSCOM)

| | |
|---|---|
| **Para qué sirve** | Resumir lo que M2 ya resuelve, señalar sus archivos importantes y dar a quien genere M3 (SDD o código) todo lo necesario para escribir el adaptador de SYSCOM sobre el contrato de M2. No sustituye a 13 ni a 14: los resume y los orienta hacia M3. |
| **Estado de M2** | Implementado el 2026-10-05 en la rama `feature/contrato-y-orquestacion`: 86 pruebas, 100 % de líneas y ramas, 21 mutaciones detectadas. |
| **Fuentes** | El código de [`apps/providers/`](../apps/providers/) manda; si este documento discrepa, manda el código. [13-sdd-m2](13-sdd-m2-contrato-y-orquestacion.md) es el diseño, [14-contrato-y-orquestacion](14-contrato-y-orquestacion.md) explica el funcionamiento en detalle, [02 §8](02-modelo-de-datos.md) recoge la API de SYSCOM y [DER](DER.md), los modelos de `catalog`. |
| **Fecha** | 2026-10-05 |

## 1. M2 en una página

M2 es la puerta única a las APIs de los proveedores. Quien consume llama a `search_all` y recibe un resultado por proveedor activo, con sus ofertas en un formato común. **Todo lo que es igual para cualquier proveedor ya está hecho; un adaptador solo aporta lo específico de su API.**

Lo que M2 garantiza a cualquier adaptador, sin que el adaptador tenga que programarlo:

| Garantía | Dónde vive |
|---|---|
| Consulta en paralelo a todos los proveedores activos (`asyncio.gather`), con un `httpx.AsyncClient` compartido | `service.py` |
| Timeout por proveedor (`Provider.timeout_ms`, editable en el admin); un proveedor lento no frena a los demás | `service.py` |
| Errores como datos: cada fallo llega dentro de `ProviderResult.error`, nunca como excepción (salvo la cancelación, que se propaga) | `service.py`, `errors.py` |
| Token OAuth2 guardado en `ProviderToken`, reutilizado mientras falten más de 5 min y renovado si no; ante un 401, se invalida y se reintenta **una** vez | `tokens.py`, `service.py` |
| Ofertas inválidas descartadas y contadas (`discarded`), sin tumbar al proveedor | `service.py` |
| Dinero siempre `Decimal`: JSON con `parse_float=Decimal`; los float se rechazan | `adapters/base.py`, `contracts.py` |
| Credenciales leídas de `.env` (`PROVIDER_<CODE>_<CLAVE>`); tokens y credenciales nunca en logs ni mensajes | `adapters/base.py`, `tokens.py` |
| Registro de adaptadores por `code` al arrancar Django | `registry.py`, `apps.py` |

```
vista async / catalog ──ProductQuery──► search_all ──► registry ──► adapters/syscom.py (M3)
                       ◄─list[ProviderResult]──┘          │              │ fetch / parse_offer
                                                      tokens.py ◄──► ProviderToken (BD)
                                                                         │ httpx
                                                                         ▼
                                                              API de SYSCOM
```

## 2. Archivos importantes

### 2.1 Código de M2 (en `apps/providers/`)

| Archivo | Líneas | Qué contiene | Símbolos clave | ¿Lo toca M3? |
|---|---|---|---|---|
| `contracts.py` | 73 | Datos que entran y salen | `ProductQuery(term, sku, limit=60)`, `ProviderOffer`, `ProviderResult`, `DEFAULT_LIMIT`, `MAX_LIMIT` | No: los **usa** |
| `errors.py` | 43 | Errores comunes | `ProviderError`, `ProviderTimeout`, `ProviderAuthError(status)`, `ProviderRateLimitError(retry_after)`, `ProviderResponseError`, `ProviderConfigError` | No: lanza `ProviderAuthError` en `request_token` |
| `adapters/base.py` | 124 | **El contrato del adaptador** y sus auxiliares | `ProviderAdapter`, `NewToken`, `get_credentials`, `parse_json`, `parse_price`, `raise_for_status` | No: **hereda** de aquí |
| `adapters/__init__.py` | 6 | Lista de adaptadores reales | (vacío) | **Sí:** añade `from . import syscom  # noqa: F401` |
| `registry.py` | 28 | `code` → adaptador | `register`, `get_adapter_class`, `registered_codes` | No: usa `@register` |
| `tokens.py` | 65 | Token OAuth2 en BD | `get_token`, `invalidate_token`, `RENEWAL_MARGIN` | No: lo usa a través de `self.token(client)` |
| `service.py` | 122 | Orquestación | `search_all(query, *, transport=None)` | No |
| `apps.py` | 11 | Carga los adaptadores al arrancar | `ProvidersConfig.ready()` | No |

M2 está **congelado**: `contracts`, `errors`, la clase `ProviderAdapter`, `register` y `search_all` son contratos. Si M3 necesitara cambiarlos, el cambio se revisa aparte; no se hace dentro del adaptador.

### 2.2 Piezas de otros módulos que M3 usa

| Archivo | Qué aporta a M3 |
|---|---|
| `apps/providers/models.py` (M1a) | `Provider` (fila `code="syscom"`, `base_url`, `timeout_ms`, `active`), `ProviderToken` (lo maneja el núcleo) y `RawProviderProduct.objects.aupsert(...)` (solo si se aprueba guardar el crudo, D3) |
| `apps/catalog/models.py` (equipo + duda 7 de M2) | `Supplier.provider`: el distribuidor "SYSCOM" del catálogo se enlaza con su `Provider` |
| `apps/catalog/constants.py` y `normalizer.py` (equipo) | El emparejamiento usa `mpn` y `brand` + `model` exactos de la oferta: M3 debe rellenarlos bien |

### 2.3 Pruebas y utilidades que M3 debe reutilizar

| Archivo | Para qué le sirve a M3 |
|---|---|
| `apps/providers/tests/soporte.py` (209 líneas) | `ApiFalsa` (API simulada sobre `httpx.MockTransport`: responde por host y ruta y guarda las peticiones en `peticiones`), `adaptadores_registrados(...)`, `respuesta_json`, `RelojControlado`, `CENTINELA_TOKEN` y `crear_proveedor`. `AdaptadorDePrueba` sirve de ejemplo de adaptador completo |
| `apps/providers/tests/test_service.py` (493 líneas) | Modelo de pruebas de integración a través de `search_all`: 401 con renovación, 403, 429, timeouts, descartes y secretos en logs |
| `apps/providers/tests/test_helpers_adaptador.py` | Cómo se prueban `parse_json`, `parse_price` y `raise_for_status` |

### 2.4 Documentos

| Documento | Cuándo leerlo |
|---|---|
| [13-sdd-m2](13-sdd-m2-contrato-y-orquestacion.md) | Firmas exactas (§4), casos límite (§8) y decisiones (§12) |
| [14-contrato-y-orquestacion](14-contrato-y-orquestacion.md) | Funcionamiento paso a paso, conexiones con los demás componentes y cómo añadir un proveedor |
| [02 §8](02-modelo-de-datos.md) | API de SYSCOM verificada, condiciones de uso (§8.2), cómo encaja con los modelos (§8.3) y preguntas abiertas para M3 (§8.4) |
| [DER](DER.md) | `Supplier`, `Product`, `Quote` y la relación con `Provider` |

## 3. El contrato que M3 tiene que cumplir

```python
@register
class SyscomAdapter(ProviderAdapter):
    code = "syscom"                                        # igual a Provider.code
    required_credentials = ("CLIENT_ID", "CLIENT_SECRET")  # → PROVIDER_SYSCOM_CLIENT_ID / _CLIENT_SECRET
    uses_token = True

    async def request_token(self, client) -> NewToken: ...       # endpoint de tokens de SYSCOM
    async def fetch(self, query, client) -> list[dict]: ...      # elementos crudos de /productos
    def parse_offer(self, item) -> ProviderOffer: ...            # un elemento → una oferta
```

| Método | Debe | No debe |
|---|---|---|
| `request_token` | Hacer el POST a SYSCOM, traducir 400/401 a `ProviderAuthError(status=…)`, usar `raise_for_status`/`parse_json` y devolver `NewToken(access_token, expires_in)` | Guardar el token (lo hace el núcleo) ni registrarlo en logs |
| `fetch` | Obtener el token con `await self.token(client)`, llamar a la API con parámetros **fijos y deterministas**, usar `raise_for_status` y `parse_json` y devolver la lista cruda de productos | Reintentar, poner timeouts, capturar errores genéricos (el núcleo los traduce) ni usar `requests`/cliente síncrono |
| `parse_offer` | Construir `ProviderOffer(provider_code=self.code, external_id=str(…), name, price=parse_price(…), currency ISO, brand, model, mpn, stock, raw=item)` | Usar `float` ni otro `provider_code` (el núcleo la descartaría). Si el elemento es inválido, lanzar `ProviderResponseError`, `ValueError`, `TypeError` o `KeyError`: el núcleo lo descarta y lo cuenta |

**Credenciales.** El núcleo entrega `self.credentials` como `{"CLIENT_ID": …, "CLIENT_SECRET": …}`, con las claves de `required_credentials`. SYSCOM espera `grant_type=client_credentials`, `client_id` y `client_secret` en minúsculas, así que `request_token` arma el formulario a mano. `AdaptadorDePrueba` manda `self.credentials` tal cual y no sirve de modelo en este punto.

**Lo que no hay que reprogramar en M3:** paralelismo, timeouts, renovación y reintento del token, la política de ofertas inválidas, la traducción de errores HTTP y de conexión, ni el registro al arrancar.

## 4. Cómo se ve desde fuera (extremo a extremo)

```python
from apps.catalog.models import Supplier
from apps.providers.contracts import ProductQuery
from apps.providers.service import search_all

for resultado in await search_all(ProductQuery(term="cámara ip", limit=20)):
    if not resultado.ok:
        continue                                   # p. ej. ProviderTimeout de syscom
    # Duda 7 de M2 (A). Supplier.DoesNotExist si ningún Supplier está enlazado.
    supplier = await Supplier.objects.aget(provider__code=resultado.provider_code)
    for oferta in resultado.offers:                # precio Decimal, moneda ISO, brand/model/mpn
        ...
```

## 5. La API de SYSCOM traducida al contrato de M2

### 5.1 Datos verificados (2026-10-04)

El detalle y las fuentes están en [02 §8.1](02-modelo-de-datos.md).

- **URL base:** `https://developers.syscom.mx/api/v1`. Va en `Provider.base_url`.
- **Token:**
  - `POST /api/v1/oauth/token`, *form-urlencoded*, con `grant_type=client_credentials`, `client_id` y `client_secret`.
  - Dura **un año**, y su endpoint tiene límites de tasa más estrictos.
  - Se envía en la cabecera `Authorization: Bearer <token>`; los productos exigen el scope `ver-productos`.
- **Productos:**
  - `GET /productos` con `busqueda`, `categoria`, `marca`, `orden`, `pagina` (1–1000), `limit` (**10**–1000, por defecto 60), `stock`, `moneda` (`usd`/`mxn`), `iva` e `inventarios`.
  - `GET /productos/{id}`, que admite hasta 300 ids separados por comas.
  - `GET /productos?modelo={modelo}`.
- **Moneda:** USD por defecto. El tipo de cambio es público en `GET /api/v1/tipocambio`.
- **Errores:**
  - 401 por el token; 403 por la cuenta o el scope; 429 con `Retry-After`.
  - El cuerpo de error es `{"error", "code", "values"}`.
  - Ya los traduce `raise_for_status`.
  - El 429 **no se reintenta**. 02 §8.3 proponía esperar el `Retry-After`, pero M2 lo devuelve como `ProviderRateLimitError(retry_after)` sin esperar (duda 3 de M2). El adaptador tampoco espera.

### 5.2 De `ProductQuery` a los parámetros de SYSCOM

| `ProductQuery` | SYSCOM | Nota para M3 |
|---|---|---|
| `term` | `busqueda` | Según /docs/productos, como mucho 120 caracteres o 10 palabras (por confirmar) (D5) |
| `sku` | `GET /productos?modelo={sku}` | Búsqueda exacta por modelo (D4) |
| `limit` (1–1000) | `limit` (10–1000) | SYSCOM no admite menos de 10 (D5) |
| — | `moneda`, `iva`, `inventarios`/`stock` | **Siempre los mismos valores** (D2, D7). Si cambian, el contenido cambia de una consulta a otra ([02 §8.3](02-modelo-de-datos.md)) |

### 5.3 De la respuesta de SYSCOM a `ProviderOffer` (todo **por confirmar**)

La documentación de SYSCOM no publica ejemplos de respuesta. La tabla es una hipótesis que hay que validar con la **primera llamada real** o con su colección de Postman.

| `ProviderOffer` | Campo probable de SYSCOM | Verificar |
|---|---|---|
| `external_id` | `id` o `producto_id` | Cuál es la clave y si es numérica (`str()` siempre) |
| `name` | `titulo` | — |
| `brand` | `marca` | ¿Texto o un objeto? |
| `model` / `mpn` | `modelo` | ¿El modelo de SYSCOM es el número de parte del fabricante? Decide qué casilla del emparejamiento se usa |
| `price` | algún campo dentro de `precios` | Cuál es el precio de distribuidor y si llega como texto o como número |
| `currency` | según el parámetro `moneda` | Fija, por los parámetros deterministas (D2) |
| `stock` | `existencia` / `total_existencia` | Solo si se piden inventarios (D7) |
| `raw` | el elemento completo | — |

## 6. Decisiones que debe tomar el SDD de M3

Incluyen las preguntas abiertas de [02 §8.4](02-modelo-de-datos.md); su pregunta 8 ya está respondida, porque es M2. Cada decisión lleva alternativas alineadas con AGENTS.md, y la recomendada está en **negrita**.

**D0. Precondición de negocio. Bloquea M3** (preguntas 1 y 2 de 02 §8.4).
- El acuerdo de uso de SYSCOM prohíbe "alimentar motores de comparación de precios masivos".
- CCONOR debe confirmar con SYSCOM, mejor por escrito, que su uso interno está permitido, y conseguir `client_id`/`client_secret` con el scope `ver-productos` ([02 §8.2](02-modelo-de-datos.md)).
- Sin credenciales solo se puede avanzar con pruebas sin red.

**D1. Modo de consulta** (pregunta 3).
- **A. Solo búsqueda bajo demanda a través de `search_all`.** Es lo que el contrato de M2 ya soporta, y no exige guardar el catálogo ni programar tareas.
- B. Descargar el catálogo completo cada cierto tiempo, con un comando de gestión y `aupsert`. Permite emparejar sin esperar a la API, pero:
  - necesita permiso para guardar el catálogo (D0, D3);
  - hay que programarlo sin dependencias nuevas;
  - necesita un upsert por lotes, que aún no existe ([02 §8.3](02-modelo-de-datos.md), punto 6).
- C. Las dos. Duplica el trabajo de M3.

**D2. Moneda e IVA** (pregunta 4; P4 del SDD de M1).
- Al adaptador solo le toca pedir siempre lo mismo y declarar la moneda en `currency`. La conversión y el IVA son cosa de `quotes` ([14](14-contrato-y-orquestacion.md)).
- **A. Pedir en USD (lo que SYSCOM da por defecto) y sin IVA.** `quotes` convierte con un tipo de cambio explícito y aplica `Quote.tax_rate` una sola vez, porque calcula `tax = subtotal × tax_rate` ([DER](DER.md)). Todos los proveedores se comparan con la misma regla.
- B. Pedir `moneda=mxn`. Es más simple y da lo que SYSCOM cobraría en pesos, pero con su propio tipo de cambio, que no coincide con el de otros proveedores.
- C. Pedir las dos monedas. Duplica las peticiones.
- El valor exacto del parámetro `iva` está por confirmar. La decisión es de negocio.

**D3. Persistencia del crudo** (decisión 2 de M2 y pregunta 6).
- **A. No guardar nada hasta que SYSCOM lo permita (D0).**
- B. Guardar cada oferta con `RawProviderProduct.objects.aupsert(provider, oferta.external_id, oferta.raw)` cuando se confirme ([14](14-contrato-y-orquestacion.md)). En ese caso hay que decidir si las existencias van en el payload, porque cuentan para el hash ([02 §8.3](02-modelo-de-datos.md), punto 4).

**D4. Búsqueda por `sku`.**
- **A. `sku` → `GET /productos?modelo=…`.** Es una búsqueda exacta y encaja con el emparejamiento por marca y modelo exactos.
- B. Mandarlo como texto en `busqueda`. Los resultados serían aproximados.
- C. Ignorarlo. Se perdería un dato que `ProductQuery` sí admite.
- Si llegan `term` y `sku` a la vez, el SDD decide cuál manda. Lo recomendado es `sku`, porque es exacto.

**D5. `limit` por debajo de 10 y búsquedas largas.**
- **A. Pedir `max(limit, 10)` y recortar el resultado a `limit`; truncar `busqueda` a lo que admite SYSCOM.** La consulta nunca falla por un límite de SYSCOM.
- B. Devolver un error para ese proveedor. El usuario se quedaría sin los precios de SYSCOM por un detalle de formato.
- C. Ignorar `limit`. Rompe el comportamiento uniforme que fijó la duda 8 de M2.

**D6. Paginación. No hay que decidir nada.** `MAX_LIMIT` (1000) coincide con el máximo por página de SYSCOM, así que **una sola página** cubre cualquier `limit` válido. Solo habría que paginar con D1-B.

**D7. Existencias** (pregunta 6).
- **A. Pedirlas y rellenar `stock` con el total**, cuando se confirme el campo. El cotizador puede preferir a quien tiene existencias.
- B. No pedirlas (`stock=None`). La respuesta es más ligera.
- C. Dejarlas solo en `raw`. El emparejamiento no las vería.

**D8. Alta del `Provider` y del `Supplier`** (pregunta 7).
- **A. Migraciones de datos, reproducibles en todos los entornos.**
  - Una en `providers` crea `Provider(code="syscom", name="SYSCOM", base_url="https://developers.syscom.mx/api/v1")`.
  - Otra en `catalog` enlaza el `Supplier` "SYSCOM", porque la dependencia va de `catalog` a `providers` (regla G4).
- B. Darlos de alta a mano desde el admin. No hace falta migración, pero cada entorno se configura por separado.
- En los dos casos, `timeout_ms` empieza en 8000 y se ajusta tras medir la API real.

**D9. Muestras de respuesta para las pruebas** (pregunta 5).
- **A. JSON de ejemplo saneados, sacados de la primera llamada real** y guardados junto a las pruebas. Mientras no haya credenciales (D0), se empieza con B y luego se sustituyen.
- B. Respuestas escritas a mano según la documentación. Hay que revisarlas cuando llegue la primera respuesta real.

**D10. Marca o distribuidor en `Supplier`.** Es la parte abierta de la duda 7 de M2, y la decide el equipo. No bloquea M3, pero cambia qué `Supplier` se enlaza en D8.

## 7. Plan de pruebas sugerido para M3

Sin red, con el runner de Django y reutilizando `tests/soporte.py` (AGENTS.md §Pruebas):

1. **`request_token`:**
   - con un 200 → `NewToken` con el token y la vida que devuelve SYSCOM;
   - con 400 y con 401 → `ProviderAuthError` con su `status`;
   - comprobar que el POST lleva `grant_type=client_credentials`, `client_id` y `client_secret`.
2. **`fetch`:**
   - comprobar en `ApiFalsa.peticiones[i].url.params` los parámetros enviados: `busqueda`, el `limit` ajustado y `moneda` e `iva` fijos;
   - comprobar la cabecera `Bearer`.
3. **`parse_offer`:**
   - un elemento de muestra → la oferta esperada, con precio `Decimal`, `external_id` en texto, marca y modelo;
   - elementos inválidos → la excepción que el núcleo descarta.
4. **Integración a través de `search_all`,** con el `SyscomAdapter` real registrado mediante `adaptadores_registrados(SyscomAdapter)` y una `ApiFalsa`:
   - resultado `ok`;
   - 401 → renovación y reintento;
   - 403;
   - 429 con `Retry-After`;
   - ofertas descartadas;
   - el token y las credenciales no aparecen en los logs.
5. **Aceptación:**
   - 100 % de `adapters/syscom.py`;
   - comprobación por mutaciones;
   - **una verificación manual contra la API real** con credenciales, documentada. Es la única que necesita red.

```python
class SyscomEnBusquedaTests(TestCase):
    async def test_una_busqueda_devuelve_ofertas_de_syscom(self):
        api = ApiFalsa()
        api.responder("developers.syscom.mx", "/api/v1/oauth/token",       # campos por confirmar (§5.3)
                      respuesta_json({"access_token": "tok-1", "expires_in": 31536000}))
        api.responder("developers.syscom.mx", "/api/v1/productos",
                      respuesta_json(MUESTRA_PRODUCTOS))                    # muestra saneada (D9)
        await Provider.objects.acreate(code="syscom", name="SYSCOM",
                                       base_url="https://developers.syscom.mx/api/v1")
        with adaptadores_registrados(SyscomAdapter), mock.patch.dict(os.environ, CREDENCIALES):
            resultados = await search_all(ProductQuery(term="camara"), transport=api.transport)
        ...
```

## 8. Archivos que creará o tocará M3 (propuesta)

| Crea | Toca | No toca |
|---|---|---|
| `apps/providers/adapters/syscom.py` | `apps/providers/adapters/__init__.py` (el import) | `contracts.py`, `errors.py`, `adapters/base.py`, `registry.py`, `tokens.py` y `service.py` (M2 está congelado) |
| `apps/providers/tests/test_adaptador_syscom.py` y las muestras JSON (nombres en *snake_case*, regla G5) | `.env_example` (solo los nombres `PROVIDER_SYSCOM_CLIENT_ID` y `PROVIDER_SYSCOM_CLIENT_SECRET`) | `apps/quotes/**`, ni `apps/catalog/**` salvo la migración de D8 |
| Las migraciones de datos de D8, si se elige A | `MEMORY.md` y `AGENTS.md`, si aparecen reglas nuevas | `settings/**`, `requirements*.txt` |
| `docs/16-sdd-m3-syscom.md` (SDD) y `docs/17-adaptador-syscom.md` (documentación final, AGENTS.md §Forma de trabajar) | | |

## 9. Criterio de aceptación sugerido para M3

| Comprobación | Resultado esperado |
|---|---|
| `python manage.py check` | Sin errores; `syscom` aparece en `registered_codes()` |
| `python manage.py makemigrations --check --dry-run` | Sin cambios de modelo (solo las migraciones de datos de D8, si se eligen) |
| `python manage.py test` | Todo en `OK`, incluidas las 9 reglas G1–G9 (sin float, sin Cisco, snake_case…) |
| Cobertura de `adapters/syscom.py` | 100 % de líneas y ramas |
| Mutaciones | Cada regla de §3 y cada decisión de §6 tiene una prueba que falla si se rompe |
| Llamada real (manual, con credenciales) | `search_all` devuelve ofertas de SYSCOM en `Decimal`, con los campos de §5.3 confirmados |
| Documentación | `docs/17-adaptador-syscom.md` explica el adaptador y cómo se conecta |

## 10. Prompt para generar el SDD de M3

```text
Genera el SDD de M3 (integración de la API de SYSCOM) del Cotizador CCONOR.

Lee antes: AGENTS.md (manda sobre todo), MEMORY.md, docs/15-guia-m2-para-m3-syscom.md,
docs/13-sdd-m2-contrato-y-orquestacion.md, docs/14-contrato-y-orquestacion.md,
docs/02-modelo-de-datos.md §8 (sobre todo §8.4) y docs/DER.md.
Comprueba con ls y grep que lo que describes existe en apps/providers/.

Antes de escribir código plantea lo siguiente:
1.- Cómo lo vas a implementar.
2.- Qué archivos vas a crear o tocar, respetando AGENTS.md (M2 está congelado).
3.- Los casos límite y las dudas que debo decidir antes de empezar, con alternativas
    alineadas con AGENTS.md y una recomendada. Parte de las decisiones D0–D10 de docs/15 §6.
4.- Qué actualizarías en AGENTS.md y MEMORY.md.
No se modifica nada hasta tener el plan aprobado.

Reglas:
- El adaptador hereda de ProviderAdapter y no reimplementa timeouts, reintentos, tokens
  ni la política de ofertas inválidas.
- Dinero en Decimal, httpx async, sin TaskGroup, sin red en las pruebas, runner de Django.
- Lo que SYSCOM no documenta (estructura de la respuesta) va como "por confirmar"; no lo inventes.
- La última tarea del plan es documentar la funcionalidad en docs/17-adaptador-syscom.md.

El SDD sigue las mismas 13 secciones que docs/13-sdd-m2-contrato-y-orquestacion.md
y se guarda en docs/16-sdd-m3-syscom.md.
```
