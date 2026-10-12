# 17 · Adaptador de SYSCOM (M3): cómo funciona y cómo se conecta

| | |
|---|---|
| **Fuente de verdad** | [`apps/providers/adapters/syscom.py`](../apps/providers/adapters/syscom.py). Si este documento y el código discrepan, manda el código |
| **Diseño y decisiones** | [16-sdd-m3-syscom.md](16-sdd-m3-syscom.md) (decisiones D1–D16 en §12 y registro de llamadas reales en §13) |
| **Estado** | Implementado y verificado contra la API real el 2026-10-05: 31 pruebas (S1–S27), 100 % de líneas y ramas, 44 mutaciones del adaptador y 4 de la red de seguridad detectadas |
| **Se apoya en** | El contrato y la orquestación de M2 ([14](14-contrato-y-orquestacion.md)) y los modelos de M1a ([02](02-modelo-de-datos.md)) |

## 1. Para qué sirve

Con M3, `search_all` ya consulta un proveedor real: SYSCOM. Una búsqueda por texto o por modelo devuelve sus productos como `ProviderOffer`:
- precio en `Decimal`, en USD y sin IVA;
- marca, modelo y MPN, para el emparejamiento del catálogo;
- existencias.

El adaptador solo aporta lo específico de SYSCOM. Lo demás (paralelismo, timeouts, token, reintento ante un 401, errores como datos y descartes) lo resuelve el núcleo de M2.

## 2. Piezas

| Archivo | Qué contiene |
|---|---|
| `apps/providers/adapters/syscom.py` | `SyscomAdapter` (`@register`, `code="syscom"`) y sus constantes: `PARAMETROS_FIJOS`, `MONEDA`, `LIMITE_MINIMO`, `MAX_PALABRAS`, `MAX_CARACTERES`, `CAMPO_PRECIO` y `SIN_PRODUCTO` |
| `apps/providers/adapters/__init__.py` | `from . import syscom`: lo registra al arrancar Django (`ProvidersConfig.ready()`) |
| `apps/providers/tests/test_adaptador_syscom.py` | Pruebas S1–S27, sin red |
| `apps/providers/tests/muestras/syscom_*.json` | Respuestas con la **estructura real** de la API y **valores ficticios** |
| `apps/providers/tests/soporte.py` | `sin_red()` y `credenciales_de_prueba()`: la red de seguridad de las pruebas (§8) |
| `.env_example` | Los nombres `PROVIDER_SYSCOM_CLIENT_ID` y `PROVIDER_SYSCOM_CLIENT_SECRET`, sin valores |

## 3. Cómo funciona una búsqueda

```
await search_all(ProductQuery(term="camara ip"))         # o ProductQuery(sku="DS-2CD1143G2-LIUF")
  └─ Provider "syscom" activo → registry → SyscomAdapter(provider, credenciales de .env)
       └─ fetch
            ├─ token: el de ProviderToken (dura un año) o, si no hay, request_token
            ├─ GET {base_url}/productos?busqueda=…&limit=…&moneda=usd&iva=0
            │   o GET {base_url}/productos?modelo=…&moneda=usd&iva=0
            ├─ 404 product_not_available (solo con modelo) → sin resultados
            └─ lista de productos, recortada a limit
       └─ parse_offer de cada producto (los inválidos se descartan y se cuentan)
  ◄─ ProviderResult("syscom", ofertas, error, elapsed_ms, discarded)
```

### 3.1 Qué se envía a SYSCOM

| `ProductQuery` | Petición | Detalle |
|---|---|---|
| `term` | `busqueda` | Se recorta a 10 palabras y 120 caracteres, el límite de SYSCOM |
| `limit` | `limit` | SYSCOM admite de 10 a 1000. Por debajo de 10 se piden 10 y se devuelven `limit` |
| `sku` | `modelo` | Búsqueda exacta. Si llegan `sku` y `term`, **manda `sku`**. SYSCOM devuelve un único objeto, y un modelo que no vende da 404 `product_not_available`, que cuenta como sin resultados |
| — | `moneda=usd`, `iva=0` | Siempre los mismos valores |

No se envía `stock=true`, porque solo devolvería productos con existencias, ni `inventarios`, porque haría la respuesta más pesada.

### 3.2 De un producto de SYSCOM a `ProviderOffer`

| `ProviderOffer` | Campo de SYSCOM | Regla |
|---|---|---|
| `external_id` | `producto_id` | Texto o entero; si no lo es, el producto se descarta |
| `name` | `titulo` | Sin espacios alrededor; si está vacío, se descarta |
| `price` | `precios.precio_descuento` | El precio de la cuenta de CCONOR, convertido con `parse_price` a `Decimal` exacto. Un precio inválido, negativo o **0** descarta el producto |
| `currency` | — | `"USD"` |
| `brand` | `marca` | Texto sin espacios alrededor; si no hay, `None` |
| `model` y `mpn` | `modelo` | El mismo valor en los dos: es el número de parte del fabricante (p. ej. `DS-2CD1143G2-LIUF` de HIKVISION) |
| `stock` | `total_existencia` | Entero ≥ 0; si no lo es, `None` (el producto **no** se descarta) |
| `raw` | el producto completo | Incluye categorías, imágenes, enlaces, desglose de existencias y precios por volumen |

## 4. Token y credenciales

**Credenciales:**
- están en `.env` como `PROVIDER_SYSCOM_CLIENT_ID` y `PROVIDER_SYSCOM_CLIENT_SECRET` (patrón `PROVIDER_<CODE>_<CLAVE>`);
- si faltan, el núcleo devuelve `ProviderConfigError` sin llamar a SYSCOM;
- nunca aparecen en logs ni en mensajes.

**Token:**
- `request_token` lo pide a `POST {base_url}/oauth/token`, con `grant_type=client_credentials`, `client_id` y `client_secret`;
- SYSCOM responde con `{"token_type": "Bearer", "expires_in": 31536000, "access_token": …}`: dura **un año**;
- el núcleo lo guarda en `ProviderToken` y lo reutiliza hasta 5 minutos antes de que caduque.

**El token actual:**
- se pidió el 2026-10-05 y caduca el 2027-10-05;
- la renovación será automática: una sola petición.

**Credenciales rechazadas (400 o 401 del endpoint de tokens):**
- se lanza `ProviderAuthError` **sin `status`**, de modo que el núcleo no reintenta;
- así no se gasta otro token del límite que SYSCOM aplica por client_id e IP (D13).

## 5. Errores: qué recibe quien consume

| Situación | `ProviderResult.error` |
|---|---|
| Faltan las credenciales | `ProviderConfigError("Faltan credenciales: PROVIDER_SYSCOM_CLIENT_ID, …")` |
| SYSCOM rechaza las credenciales | `ProviderAuthError("SYSCOM rechazó las credenciales (HTTP 401).")`, sin reintento |
| Token revocado o caducado (401 en `/productos`) | Ninguno: el núcleo renueva el token y reintenta una vez |
| 403 (cuenta no habilitada o falta el scope `ver-productos`) | `ProviderAuthError(status=403)` |
| 429 | `ProviderRateLimitError(retry_after=…)`, sin reintento |
| 404 en una búsqueda por texto, 5xx, JSON inválido o una respuesta sin la forma esperada | `ProviderResponseError` |
| Más de `Provider.timeout_ms` (8000 por defecto) | `ProviderTimeout` |
| Búsqueda sin resultados o modelo que SYSCOM no vende | Ninguno: `ok`, con 0 ofertas |

## 6. Cómo se conecta con los demás componentes

### 6.1 Con M2 (núcleo de proveedores)

`SyscomAdapter` hereda de `ProviderAdapter`, usa `parse_json`, `parse_price` y `raise_for_status`, y se registra con `@register`. No modifica ni reimplementa nada de M2 (ver [14](14-contrato-y-orquestacion.md)).

### 6.2 Con M1a (modelos de `apps/providers`)

- **`Provider` "syscom":**
  - `base_url` = `https://developers.syscom.mx/api/v1`;
  - `timeout_ms` = 8000;
  - `active` decide si se consulta.
- **`ProviderToken`:** guarda el único token real.
- **`RawProviderProduct`:** M3 no lo usa (D3), porque falta el permiso de SYSCOM para guardar su catálogo. Si se aprueba, bastaría con `aupsert(provider, oferta.external_id, oferta.raw)` por cada oferta.

### 6.3 Con `catalog` (emparejamiento, del equipo)

- `brand`, `model` y `mpn` alimentan los métodos deterministas de `catalog/constants.py`: "MPN exacto" y "Marca y modelo exactos". Con SYSCOM, `mpn` = `model`.
- **El enlace con `Supplier` queda fuera de M3** (D8, D10). Cuando el equipo decida si `Supplier` guarda distribuidores o marcas, se hace con `Supplier.objects.filter(name="SYSCOM").update(provider=Provider.objects.get(code="syscom"))`. Después, `Supplier.objects.aget(provider__code=oferta.provider_code)` encuentra el distribuidor de cada oferta.

### 6.4 Con `quotes`

- **Precios:** llegan en USD y sin IVA (`moneda=usd`, `iva=0`).
- **Lo que hace `quotes`:** convierte a la moneda de la cotización con un tipo de cambio explícito y aplica el IVA una sola vez (`tax = subtotal × tax_rate`).
- **El precio:** es `precio_descuento`, el que paga CCONOR. Nunca es el público (`precio_1` o `precio_lista`).
- **Precios de distribuidor:** el acuerdo de SYSCOM pide mostrarlos solo a usuarios que hayan iniciado sesión.

### 6.5 Con las vistas async (futuras)

```python
resultados = await search_all(ProductQuery(term=request.GET["q"]))
```

Hay un resultado por proveedor activo. El de SYSCOM llega en unos 0,2–0,8 s (medido en la verificación).

## 7. Puesta en marcha en otro entorno (equipo)

1. Copiar `.env_example` a `.env` y rellenar `PROVIDER_SYSCOM_CLIENT_ID` y `PROVIDER_SYSCOM_CLIENT_SECRET`. Las credenciales las proporciona CCONOR y **nunca** van en git.
2. Aplicar las migraciones con `python manage.py migrate`.
3. Dar de alta el proveedor en el admin (**Providers → Add**):
   - `code`: `syscom`;
   - `name`: `SYSCOM`;
   - `base_url`: `https://developers.syscom.mx/api/v1`;
   - `timeout_ms`: 8000;
   - `active`: solo cuando ya estén las credenciales.
4. La primera búsqueda pide el token y lo guarda. Para no gastar el límite de SYSCOM:
   - **no pidas tokens a mano**;
   - no borres ni invalides el `ProviderToken`;
   - no repitas búsquedas en bucle.

## 8. Cómo probarlo

```powershell
python manage.py test apps.providers.tests.test_adaptador_syscom   # S1–S27
python manage.py test                                               # todo, incluidas G1–G9
```

**Red de seguridad:**
- las pruebas **nunca** llaman a SYSCOM ni ven credenciales reales;
- `sin_red()` corta cualquier transporte real de httpx y hace fallar la prueba si alguien lo intenta;
- `credenciales_de_prueba("syscom")` retira las variables reales de `os.environ` y pone valores falsos;
- todo ocurre en la BD de pruebas;
- la API se simula con `ApiFalsa` y las muestras de `tests/muestras/`.

**Cobertura y mutaciones:** los comandos están en AGENTS.md §Pruebas. Las mutaciones se hacen con un script que restaura los bytes desde memoria, nunca con `git restore`.

## 9. Llamadas reales hechas (2026-10-05)

El presupuesto lo aprobó el usuario y el registro completo está en [16 §13](16-sdd-m3-syscom.md). Se hicieron **1 petición de token y 6 GET**, en secuencia y con pausas. No hubo ningún error ni ningún 429.

| Etapa | Llamadas | Resultado |
|---|---|---|
| Reconocimiento | Token, búsqueda, modelo y búsqueda sin resultados | Estructura real confirmada (16 §4.3); token guardado |
| Punto de control | 1 GET extra autorizado: modelo inexistente | 404 `{"error": "product_not_available"}` |
| Verificación | `search_all` por término y por `sku` | ok: 10 ofertas en 790 ms; 1 oferta (el mismo producto) en 226 ms; `Decimal` > 0, USD y MPN = modelo; **0 tokens nuevos** |

## 10. Límites conocidos y pendientes

- **No se guarda el catálogo** (D3) ni se descarga entero (D1), hasta tener el permiso de SYSCOM.
- **Enlace con `Supplier`:** pendiente de la decisión del equipo (D10).
- **Conversión de moneda e IVA:** son de `quotes` y siguen sin implementar.
- **Existencias:** en la muestra real, `total_existencia` solo tomó valores redondos (200 y 500). Puede ser un tope que aplica SYSCOM, así que conviene tratarla como indicativa.
- **Límites de peticiones de SYSCOM:** no publica cifras. Por eso nunca se reintenta un 429 y nunca se pide un token por búsqueda.
- **Precios por volumen** (`precios.volumen`, solo en algunos productos): se conservan en `raw` y no se usan.
