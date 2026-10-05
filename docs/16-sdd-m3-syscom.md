# 16 · SDD de M3: integración de la API de SYSCOM

## 1. Identificación

| | |
|---|---|
| **Módulo** | M3: adaptador de la API de SYSCOM (`apps/providers/adapters/syscom.py`) |
| **Rama** | `feature/integracion-syscom`, creada desde `main` en `a9652dc` (M2 fusionado) |
| **Estado** | Implementado y verificado contra la API real (2026-10-05). Funcionamiento en [17](17-adaptador-syscom.md) |
| **Depende de** | M2: contrato y orquestación ([13](13-sdd-m2-contrato-y-orquestacion.md), [14](14-contrato-y-orquestacion.md)); M1a: `Provider` y `ProviderToken` ([02](02-modelo-de-datos.md)) |
| **Parte de** | La guía de traspaso [15](15-guia-m2-para-m3-syscom.md). Este documento **confirma o corrige sus hipótesis** de §5.3 con la API real (ver §4.3) y toma las decisiones D1–D16 que allí quedaban abiertas (§12) |

## 2. Propósito y alcance

`SyscomAdapter` permite que `search_all` consulte el catálogo de SYSCOM y devuelva sus productos como `ProviderOffer` (precio `Decimal` en USD sin IVA, marca, modelo y MPN para el emparejamiento, y existencias).

**Entra:**
- búsqueda por texto (`busqueda`) y por modelo exacto (`sku` → `modelo`);
- token OAuth2 (`client_credentials`) gestionado por el núcleo de M2;
- protección de las credenciales en las pruebas (§6.2);
- documentación final en `docs/17`.

**No entra:**
- descargar el catálogo completo cada cierto tiempo (D1);
- guardar la respuesta cruda en `RawProviderProduct` (D3);
- enlazar el `Supplier` "SYSCOM" del catálogo (D8 y D10);
- convertir la moneda y aplicar el IVA, que son de `quotes`;
- las vistas.

## 3. Dependencias

- **Contrato de M2, congelado.** M3 hereda y no modifica:
  - `ProviderAdapter`, `NewToken`, `parse_json`, `parse_price` y `raise_for_status` (`adapters/base.py`);
  - `ProductQuery` y `ProviderOffer` (`contracts.py`);
  - los errores de `errors.py`;
  - `@register` (`registry.py`);
  - `get_token` (`tokens.py`);
  - `search_all` (`service.py`).
- **Modelos de M1a:**
  - la fila `Provider` "syscom", con su `base_url` y su `timeout_ms`;
  - `ProviderToken`, donde se guarda el único token real.
- **Configuración:** `PROVIDER_SYSCOM_CLIENT_ID` y `PROVIDER_SYSCOM_CLIENT_SECRET` en `.env` (D15).
- **Dependencias externas:** ninguna nueva. `httpx` ya estaba en `requirements.txt`.

## 4. Contratos

### 4.1 Clase

```python
@register
class SyscomAdapter(ProviderAdapter):
    code = "syscom"
    required_credentials = ("CLIENT_ID", "CLIENT_SECRET")  # PROVIDER_SYSCOM_CLIENT_ID / _CLIENT_SECRET
    uses_token = True
    async def request_token(self, client) -> NewToken
    async def fetch(self, query, client) -> list[Mapping]
    def parse_offer(self, item) -> ProviderOffer
```

### 4.2 Peticiones

| Llamada | Petición | Notas |
|---|---|---|
| `request_token` | `POST {base_url}/oauth/token`, *form-urlencoded*: `grant_type=client_credentials`, `client_id`, `client_secret` | Respuesta real: `{"token_type": "Bearer", "expires_in": 31536000, "access_token": "…"}` (un año) |
| `fetch` con `term` | `GET {base_url}/productos?busqueda=…&limit=…&moneda=usd&iva=0` | `limit = max(query.limit, 10)`. `busqueda` se recorta a 10 palabras y 120 caracteres |
| `fetch` con `sku` | `GET {base_url}/productos?modelo=…&moneda=usd&iva=0` | Manda sobre `term` (D4). Devuelve **un objeto**, no una lista |

`base_url` se usa sin la barra final. La cabecera es `Authorization: Bearer <token>`.

### 4.3 Respuestas reales (consultadas el 2026-10-05, §13)

**Búsqueda:**
- La respuesta es `{"cantidad", "pagina", "paginas", "productos": [...], "todo"}`.
- Sin resultados devuelve **200** con `"productos": []` y `cantidad` 0.

**Producto:**
- Lo que usa el adaptador:
  - `producto_id` (texto);
  - `modelo` (texto);
  - `titulo` (texto);
  - `marca` (texto);
  - `total_existencia` (entero);
  - `precios`, que es un objeto con `precio_1`, `precio_especial`, `precio_descuento`, `precio_map` y `precio_lista`, y con `volumen` solo en algunos productos. Los precios llegan como **texto decimal** (`"999.99"`).
- Lo que el adaptador ignora y conserva en `raw`:
  - `caracteristicas`, `categorias` y `categorias_producto_todas`;
  - `img_portada`, `link`, `link_privado` e `iconos` (unas veces lista y otras objeto);
  - `existencia {nuevo, asterisco{a,b,c,d}, detalle}`, donde `total_existencia` = `nuevo` + la suma de `asterisco`;
  - unidades, peso y medidas;
  - en algunos productos, `proyecto`.

**Por modelo:**
- La respuesta es el objeto del producto, con su modelo exacto, más `descripcion`, `imagenes` y `recursos`.
- Si SYSCOM no vende ese modelo, devuelve **404** con `{"error": "product_not_available"}`.

En los 10 productos de la muestra, el orden de los precios es: `precio_descuento` < `precio_map` < `precio_especial` < `precio_1` = `precio_lista`.

**Respecto a las hipótesis de [15 §5.3](15-guia-m2-para-m3-syscom.md):**
- se confirman `titulo`, `marca`, `modelo` y `precios`;
- el id es `producto_id`;
- las existencias van en `total_existencia`;
- la búsqueda por modelo devuelve un objeto, no una lista.

### 4.4 De un producto a `ProviderOffer`

| `ProviderOffer` | Origen | Regla |
|---|---|---|
| `provider_code` | `"syscom"` | — |
| `external_id` | `producto_id` | Texto o entero (no `bool`); si falta o no es válido, se descarta |
| `name` | `titulo` | Texto no vacío; si no lo es, se descarta |
| `price` | `precios.precio_descuento` (D12) | Con `parse_price`. Si es inválido, negativo o **0**, se descarta |
| `currency` | — | `"USD"` (D2) |
| `brand` | `marca` | Si no es texto o está vacía, `None` |
| `model` y `mpn` | `modelo` (D11) | Si no es texto o está vacío, `None` |
| `stock` | `total_existencia` (D7) | Entero ≥ 0 (no `bool`); si no lo es, `None`. Una oferta con existencia inválida se conserva |
| `raw` | el elemento completo | — |

## 5. Archivos

| Archivo | Acción |
|---|---|
| `apps/providers/adapters/syscom.py` | Crear |
| `apps/providers/adapters/__init__.py` | Añadir `from . import syscom  # noqa: F401` |
| `apps/providers/tests/soporte.py` | Añadir `sin_red()` y `credenciales_de_prueba()` |
| `apps/providers/tests/test_adaptador_syscom.py` | Crear (pruebas S1–S27) |
| `apps/providers/tests/muestras/syscom_*.json` | Crear: estructura real con **valores ficticios** |
| `.env_example` | Añadir los nombres `PROVIDER_SYSCOM_CLIENT_ID` y `PROVIDER_SYSCOM_CLIENT_SECRET` |
| `docs/16` (este) y `docs/17-adaptador-syscom.md` | Crear |

## 6. Diseño

### 6.1 Flujo

```
search_all ──► SyscomAdapter.fetch
                 ├─ self.token(client) ──► ProviderToken (vigente un año) o request_token
                 ├─ GET /productos (busqueda | modelo) + moneda=usd + iva=0
                 ├─ 404 product_not_available (solo con modelo) → []
                 └─ lista (busqueda) o [objeto] (modelo), recortada a limit
           ──► parse_offer por elemento (los inválidos se descartan y se cuentan)
```

### 6.2 Protección de las credenciales

Las pruebas automáticas, incluidas la cobertura y las mutaciones, **nunca** salen a la red ni ven credenciales reales:
- usan `ApiFalsa` (`httpx.MockTransport`);
- `sin_red()` corta y hace fallar cualquier uso de un transporte real;
- `credenciales_de_prueba("syscom")` retira de `os.environ` las variables reales y pone valores falsos;
- todo ocurre en la BD de pruebas (`exigir_bd_de_pruebas()`).

**Llamadas reales:**
- solo con un presupuesto aprobado, en secuencia, con pausas y parando ante el primer 401, 403, 429 o 5xx;
- **un solo token** en todo M3, guardado en `ProviderToken` y reutilizado;
- nunca se imprimen ni se guardan en el repositorio tokens, credenciales ni precios reales.

El registro de las llamadas está en §13.

## 7. Reglas duras aplicables

AGENTS.md:
- todo dentro de `apps/providers/`;
- sin dependencias nuevas;
- `httpx` asíncrono;
- dinero en `Decimal`, nunca `float(`;
- credenciales solo en `.env`;
- el token nunca en logs ni mensajes;
- runner de Django;
- G1–G9 en verde.

M2 congelado: el adaptador no reimplementa timeouts, reintentos, gestión del token ni la política de descartes.

## 8. Errores y casos límite

| Caso | Resultado |
|---|---|
| El endpoint de tokens responde 400 o 401 | `ProviderAuthError` **sin `status`** (D13): el núcleo no reintenta y no se gasta otro token |
| El endpoint de tokens responde 429 o 5xx | `ProviderRateLimitError` o `ProviderResponseError` (`raise_for_status`) |
| La respuesta del token no tiene `access_token` o `expires_in` válidos, o no es JSON | `ProviderResponseError("Respuesta de token inválida.")`, sin su contenido |
| 401 en `/productos` | El núcleo invalida el token, lo renueva y reintenta una vez |
| 403 / 429 / 5xx en `/productos` | Error sin reintento (con `retry_after` en el 429) |
| 404 en la búsqueda por texto | Error "HTTP 404 del proveedor." (D14): SYSCOM responde 200 vacío cuando no hay resultados |
| 404 `product_not_available` en la búsqueda por modelo | `[]` (ok, 0 ofertas). Cualquier otro 404 es error |
| Respuesta sin lista de productos, o la búsqueda por modelo no devuelve un objeto | `ProviderResponseError` |
| Elemento inválido (§4.4) | Se descarta y se cuenta en `discarded` |
| `term` y `sku` a la vez | Manda `sku` |
| `term` de más de 10 palabras o 120 caracteres | Se recorta |
| `limit` < 10 | Se piden 10 y se devuelven `limit` |
| Faltan las credenciales | `ProviderConfigError` del núcleo, sin ninguna petición |

## 9. Plan de pruebas (`test_adaptador_syscom.py`)

**Red de seguridad:**
- S1: `sin_red()` corta un transporte real (síncrono y asíncrono) y hace fallar la prueba.
- S2: `credenciales_de_prueba()` oculta las variables reales y las restaura al salir.

**`request_token`:**
- S3: un 200 → `NewToken`. El POST lleva el formulario correcto, en minúsculas.
- S4: 400 y 401 → `ProviderAuthError` sin `status` y sin el secreto en el mensaje.
- S5: 429 → `ProviderRateLimitError(retry_after)`; 500 → `ProviderResponseError`.
- S6: una respuesta de token inválida → `ProviderResponseError` sin el token.

**`fetch`:**
- S7: la búsqueda envía `busqueda`, `limit`, `moneda=usd`, `iva=0` y `Bearer`, y reutiliza el token guardado.
- S8: `sku` → `modelo` (manda sobre `term`), sin `limit`; el objeto se devuelve en una lista.
- S9: `busqueda` se recorta a 10 palabras y 120 caracteres.
- S10: con `limit` < 10 se piden 10 y se devuelven `limit`.
- S11: una respuesta sin lista, o un modelo que no es objeto → `ProviderResponseError`.
- S12: 404 `product_not_available` con modelo → `[]`.
- S13: cualquier otro 404 (otro cuerpo, sin JSON, o la búsqueda por texto) → error.
- S14: `base_url` con barra final → rutas sin doble barra.

**`parse_offer`:**
- S15: la muestra produce la oferta esperada (`Decimal` exacto, MPN = modelo, existencias y USD).
- S16: los elementos inválidos (id, título, precios, precio 0 o negativo, elemento que no es objeto) lanzan excepciones que el núcleo descarta.
- S17: una existencia, una marca o un modelo inválidos quedan en `None`; el 0 en existencias es válido; título, marca y modelo llegan sin espacios alrededor.

**`search_all` con el adaptador real:**
- S18: búsqueda completa con ofertas y descartes contados.
- S19: el token se reutiliza en la segunda búsqueda (un solo POST).
- S20: un 401 en `/productos` renueva el token y reintenta.
- S21: un 403 no se reintenta.
- S22: un 429 devuelve `retry_after`.
- S23: con las credenciales rechazadas se pide **un solo token**.
- S24: sin credenciales no hay ninguna petición.
- S25: un `sku` que SYSCOM no vende da `ok` con 0 ofertas.
- S26: ni el token ni el `client_secret` aparecen en los logs ni en los mensajes.

**Registro:**
- S27: `syscom` está en el registro real, con sus credenciales y `uses_token`.

## 10. Criterio de aceptación

| Comprobación | Esperado |
|---|---|
| `python manage.py check` / `makemigrations --check --dry-run` | Sin errores ni cambios de modelo |
| `python manage.py test` | Todo en OK, incluidas G1–G9 |
| `coverage run --branch …` | 100 % de líneas y ramas en `adapters/syscom.py` |
| Mutaciones (bytes restaurados desde memoria, nunca `git restore`) | Todas detectadas |
| Verificación real (Etapa 3, 2 GET) | `search_all` devuelve ofertas de SYSCOM con los campos de §4.4; 0 peticiones de token |

## 11. Tareas

| Etapa | Contenido | Estado |
|---|---|---|
| 0 | Renombrar las variables de `.env` (solo los nombres); `migrate` en desarrollo; alta del `Provider` "syscom" inactivo | Hecho |
| 1 | Reconocimiento real: 1 token y 3 GET, más 1 GET extra autorizado; punto de control D11/D12 | Hecho |
| 2 | TDD sin red: muestras, `soporte.py`, pruebas S1–S27, `syscom.py`, registro y `.env_example` | Hecho: 31 pruebas en verde (S1, S9 y S14 se reparten en varios métodos) |
| 3 | Verificación real: activar el `Provider` y lanzar `search_all` por término y por `sku` (2 GET, token reutilizado) | Hecho: ver §13 |
| 4 | Cobertura, mutaciones, `docs/17`, AGENTS.md y MEMORY.md | Hecho: 100 % de líneas y ramas (84 sentencias y 22 ramas); 44 mutaciones del adaptador y 4 de la red de seguridad, todas detectadas |

## 12. Decisiones tomadas (2026-10-05)

Todas en la alternativa recomendada. La numeración es la de [15 §6](15-guia-m2-para-m3-syscom.md); D11–D16 son nuevas.

| # | Decisión |
|---|---|
| D0 | Resuelta: hay acceso y credenciales oficiales, que solo usa este proyecto |
| D1 | Solo búsqueda bajo demanda con `search_all` |
| D2 | `moneda=usd` e `iva=0` siempre; `currency="USD"`. `quotes` convierte y aplica `tax_rate` una vez |
| D3 | No se guarda la respuesta cruda en M3 |
| D4 | `sku` → `modelo=`, búsqueda exacta; manda sobre `term` |
| D5 | `limit = max(limit, 10)` y recorte; `busqueda` como mucho 10 palabras y 120 caracteres |
| D6 | Una sola página (`MAX_LIMIT` = máximo de SYSCOM) |
| D7 | Sin `inventarios`: `stock = total_existencia`. Nunca `stock=true` |
| D8 | Alta manual del `Provider` (shell ahora; admin para el equipo, en `docs/17`). Sin migración de datos: rompería por unicidad `test_supplier_provider.py` |
| D9 | Muestras con la estructura real de §4.3 y valores ficticios |
| D10 | Fuera de M3 (lo decide el equipo) |
| D11 | **`modelo` va en `model` y en `mpn`**: los ejemplos reales son números de parte del fabricante (p. ej. de HIKVISION) |
| D12 | **Precio = `precio_descuento`**, el más bajo en todos los productos (el precio de la cuenta de CCONOR). El precio 0 se descarta |
| D13 | Credenciales rechazadas → `ProviderAuthError` sin `status` |
| D14 | 404 = error, salvo `product_not_available` en la búsqueda por modelo, que es "sin resultados" |
| D15 | Variables renombradas en `.env` a `PROVIDER_SYSCOM_CLIENT_ID` y `PROVIDER_SYSCOM_CLIENT_SECRET` |
| D16 | Las llamadas reales las hace Claude con presupuesto cerrado (§13) |

## 13. Registro de llamadas reales

Presupuesto aprobado:
- Etapa 1: 1 token y 3 GET, más 1 GET extra autorizado en el punto de control;
- Etapa 3: 2 GET.

Todas las llamadas son secuenciales, con pausas de 3 s.

| # | Fecha | Etapa | Petición | Resultado |
|---|---|---|---|---|
| 1 | 2026-10-05 | 1 | `POST /oauth/token` | 200: token Bearer de un año, guardado en `ProviderToken` (caduca el 2027-10-05). **Es el único token** |
| 2 | 2026-10-05 | 1 | `GET /productos?busqueda=camara ip&limit=10&moneda=usd&iva=0` | 200: 10 de 177 productos (18 páginas) |
| 3 | 2026-10-05 | 1 | `GET /productos?modelo=<modelo del 1.º>&limit=10&moneda=usd&iva=0` | 200: un objeto con el producto exacto |
| 4 | 2026-10-05 | 1 | `GET /productos?busqueda=<término sin resultados>&…` | 200: `productos: []`, `cantidad: 0` |
| 5 | 2026-10-05 | Extra (autorizada) | `GET /productos?modelo=<inexistente>&…` | 404: `{"error": "product_not_available"}` |
| 6 | 2026-10-05 | 3 | `search_all(ProductQuery(term="camara ip", limit=10))` | ok: 10 ofertas y 0 descartes en 790 ms; precios `Decimal` > 0, USD, MPN = modelo y existencias |
| 7 | 2026-10-05 | 3 | `search_all(ProductQuery(sku=<modelo de la 1.ª oferta>))` | ok: 1 oferta (el mismo `producto_id`) en 226 ms |

**Total: 1 petición de token y 6 GET, sin errores ni 429.** Tras la Etapa 3 el token sigue siendo el mismo (no se pidió otro). El `Provider` "syscom" queda **activo** en la BD de desarrollo.

Las respuestas crudas se analizaron fuera del repositorio. Solo se publicaron claves, tipos, recuentos y el orden relativo de los precios.
