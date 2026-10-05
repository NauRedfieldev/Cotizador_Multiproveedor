# 02 · Modelo de datos de `apps/providers` y preparación para M3 (SYSCOM)

| | |
|---|---|
| **Fuente de verdad** | [`apps/providers/models.py`](../apps/providers/models.py) (commit `738fcbc`). Si este documento y el código discrepan, manda el código. |
| **Estado** | Implementado y probado: 103 pruebas, 100 % de líneas y ramas en `models.py` y `admin.py`. |
| **Fecha** | 2026-10-04 |
| **Relacionado** | SDD de M1 ([`12-sdd-m1-providers-core.md`](12-sdd-m1-providers-core.md), §4.6) · [AGENTS.md](../AGENTS.md) §Pruebas |

## 1. Visión general

`apps/providers` guarda tres cosas:
- la configuración de cada proveedor;
- el **catálogo crudo** de cada uno, tal como lo devuelve su API, deduplicado por un hash de contenido;
- el **token de acceso** vigente.

Normalizar y comparar productos no es trabajo de esta app: eso llega después (catalog/quotes), leyendo el histórico crudo.

```
                 ┌──────────────────────┐
                 │       Provider       │  code (único) · active · base_url · timeout_ms
                 └──────────┬───────────┘
          1:N, PROTECT      │       1:1, CASCADE
        ┌───────────────────┴───────────────────┐
┌───────▼──────────────────┐        ┌───────────▼────────┐
│    RawProviderProduct    │        │   ProviderToken    │
│ único (provider,         │        │ access_token       │
│        external_id)      │        │ expires_at         │
│ payload jsonb + GIN      │        │ obtained_at        │
│ content_hash · fechas    │        └────────────────────┘
│ fetch_count              │
└──────────────────────────┘
```

Una fila de `RawProviderProduct` es **un producto de un proveedor**. Solo se escribe con `RawProviderProduct.objects.aupsert()` (§4).

## 2. Modelos

### 2.1 `Provider` (tabla `providers_provider`)

| Campo | Tipo | Reglas |
|---|---|---|
| `code` | `SlugField(32)`, único | Identificador estable (p. ej. `"syscom"`). Las variables de entorno siguen el patrón `PROVIDER_<CODE>_<CLAVE>`. |
| `name` | `CharField(120)` | Nombre visible. |
| `base_url` | `URLField` | URL base de la API. |
| `active` | `bool`, por defecto `True` | Para retirar un proveedor se pone `active=False`; si tiene histórico no se puede borrar (§5). |
| `timeout_ms` | entero positivo, por defecto `8000` | Valor provisional (SDD P5). |
| `created_at` / `updated_at` | `DateTimeField` automáticos | |

Se ordena por `code`. `str()` → `"SYSCOM (syscom)"`.

### 2.2 `RawProviderProduct` (tabla `raw_provider_product`)

| Campo | Tipo | Reglas |
|---|---|---|
| `provider` | FK a `Provider`, **`PROTECT`**, `related_name="raw_products"` | Protege el histórico al borrar proveedores. |
| `external_id` | `CharField(128)` | Id del producto en el proveedor. **Siempre texto y nunca vacío**: `aupsert` lo normaliza con `str()`. |
| `content_hash` | `CharField(64)` | SHA-256 del payload (§3). |
| `payload` | `JSONField` (jsonb) con `DjangoJSONEncoder` | La respuesta cruda del proveedor para ese producto. |
| `first_seen_at` | `DateTimeField`, **sin valor por defecto** | Lo fija `aupsert` al crear la fila. |
| `last_seen_at` | `DateTimeField` | Se actualiza en cada consulta. |
| `fetch_count` | entero positivo, por defecto 1 | **Cuenta consultas, no versiones**: suma 1 tanto si el contenido cambia como si no. |

| Restricción / índice | Tipo | Para qué |
|---|---|---|
| `raw_pp_provider_external_uniq` | `UNIQUE (provider_id, external_id)` | Una fila por producto y proveedor. Es el destino del `ON CONFLICT` de `aupsert`. |
| `raw_pp_last_seen_idx` | btree sobre `last_seen_at` | Consultas y purgas por antigüedad. |
| `raw_pp_payload_gin` | GIN sobre `payload` | Búsquedas por contenido: `filter(payload__contains={...})`, que se traduce a `@>`. |

### 2.3 `ProviderToken` (tabla `providers_providertoken`)

| Campo | Tipo | Reglas |
|---|---|---|
| `provider` | `OneToOneField` a `Provider`, `CASCADE`, `related_name="token"` | **Un token vigente por proveedor.** |
| `access_token` | `TextField` | Secreto: nunca aparece en el admin, en `str()`/`repr()` ni en los logs. |
| `expires_at` | `DateTimeField` | Caducidad. |
| `obtained_at` | `DateTimeField`, por defecto `timezone.now` | Momento en que se obtuvo. |

`str()` → `"Token de 7 (expira 2027-10-04 12:00)"`, o `"Token de 7 (expira: sin fecha)"` si falta la fecha.

## 3. Contrato del hash: `compute_content_hash(payload)`

```python
sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), cls=DjangoJSONEncoder).encode("utf-8")).hexdigest()
```

**El hash es el del JSON tal como se guarda.** Usa el mismo `DjangoJSONEncoder` que el campo `payload`, así que se cumple siempre:
`sha256(JSON canónico del payload leído de la BD) == content_hash`.

| Entrada | Efecto en el hash |
|---|---|
| Orden de las claves, también anidadas | No lo cambia. |
| Orden de los elementos de una lista | Lo cambia: es contenido. |
| `Decimal("1.1")` frente a `"1.1"` | **Mismo hash**: los dos se guardan como texto. |
| `Decimal("1.10")` frente a `Decimal("1.1")` | Distinto: se conservan los ceros. |
| `Decimal("1.1")` frente a `1.1` (float) | Distinto: texto frente a número. |
| `datetime` | Se guarda en ISO 8601 truncado a milisegundos; los microsegundos no cuentan. |
| `UUID` | Se guarda como texto. |
| `set` u otro tipo que JSON no admite | `TypeError`: tampoco podría guardarse. |

Vectores de referencia: `{"b": 2, "a": 1}` → `43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777`; `{}` → `44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a`.

## 4. Contrato de escritura: `aupsert`

```python
obj, estado = await RawProviderProduct.objects.aupsert(provider, external_id, payload)
# estado ∈ {"created", "updated", "unchanged"}
```

**Validaciones**, antes de tocar la BD:
- `provider` sin guardar → `ValueError("El proveedor debe estar guardado antes de llamar a aupsert.")`
- `external_id` se convierte con `str()`, así que `123` y `"123"` son la misma fila. Si queda vacío o solo con espacios → `ValueError("external_id no puede estar vacío.")`

| Situación | Estado | Sentencias SQL |
|---|---|---|
| Mismo contenido que lo guardado | `unchanged` | **1**: `UPDATE … RETURNING` |
| Producto nuevo | `created` | 2: el `UPDATE` no encuentra fila, y después `INSERT` |
| Contenido distinto | `updated` | 2: el `UPDATE` no encuentra el hash, y después `INSERT … ON CONFLICT DO UPDATE … WHERE content_hash <> nuevo` |

| Campo | `created` | `unchanged` | `updated` |
|---|---|---|---|
| `payload`, `content_hash` | nuevos | sin cambios | nuevos |
| `first_seen_at` | ahora | sin cambios | sin cambios |
| `last_seen_at` | ahora | ahora | ahora |
| `fetch_count` | 1 | +1 | +1 |

**Garantías:**
- **Concurrencia:** el estado es exacto aunque haya llamadas simultáneas. Con 20 altas idénticas a la vez se obtienen 1 `created` y 19 `unchanged`, una sola fila y `fetch_count = 20`, sin incrementos perdidos.
- **Reintentos:** si el `INSERT` choca con una fila que ya tiene el mismo hash, se reintenta como `unchanged`, hasta 3 veces. Después lanza `RuntimeError`.
- **Objeto devuelto:** el que se devuelve **es la fila guardada**, porque sale del `RETURNING`.
- **Implementación:** usa SQL de PostgreSQL (`ON CONFLICT`, desde la 9.5; en desarrollo hay 18.6) y lo ejecuta con `self.raw()` y `async for`. El proyecto solo admite PostgreSQL (AGENTS.md).

## 5. Decisiones y su porqué (QA de M1a, 2026-10-04)

| Decisión | Por qué |
|---|---|
| El hash es el del JSON tal como se guarda | Lo que se compara es exactamente lo que se guarda. Admite `Decimal` (SDD D4: `parse_float=Decimal`). |
| `PROTECT` en el histórico; un proveedor se retira con `active=False` | Un borrado accidental (el admin de Provider permite borrar) no puede llevarse miles de filas crudas. |
| `external_id` en texto y no vacío | PostgreSQL no compara `varchar` con `int`, y las APIs suelen dar ids numéricos. |
| El token de corta duración va en la BD; las credenciales de larga duración, solo en `.env` | El token lo comparten todos los workers y sobrevive a reinicios. El secreto de larga vida nunca toca la BD (SDD D5). |
| `aupsert` con SQL nativo en vez de `aupdate_or_create` | Una sola consulta en la ruta más frecuente (`unchanged`) y estado exacto bajo concurrencia. Corrige HALLAZGO-3 y HALLAZGO-4. |
| El admin del histórico crudo es de solo lectura y el token nunca se muestra | Integridad del histórico y seguridad. |
| Ante discrepancias, manda el código | Por eso los nombres son `code` y `active`, y no existe `moneda_default`: la moneda sigue abierta (SDD P4). |
| Dinero siempre en `Decimal`, nunca `float` | Regla del proyecto, comprobada por la prueba G2. |

## 6. Admin y migraciones

- **Admin:**
  - `Provider` es editable.
  - `RawProviderProduct` se puede ver, pero no crear, editar ni borrar, tampoco en masa.
  - `ProviderToken` excluye `access_token` y no permite altas.
- **Migraciones:**
  - `0001_initial` crea las tablas y los índices.
  - `0002_alter_rawproviderproduct_provider` cambia `on_delete` a `PROTECT` y no ejecuta SQL. **Está pendiente de aplicar en la BD de desarrollo** (`python manage.py migrate`).
  - El `GinIndex` exige `django.contrib.postgres` en `INSTALLED_APPS` (ya está).

## 7. Pruebas que lo respaldan

| Archivo (`apps/providers/tests/`) | Cubre |
|---|---|
| `test_models.py` | Pruebas originales de M1a. |
| `test_content_hash.py` | Contrato del hash (§3). |
| `test_aupsert.py` | Ramas, sentencias por rama, objeto devuelto y validaciones de `aupsert`. |
| `test_aupsert_concurrencia.py` | Concurrencia con `asyncio.gather` y con hilos reales (una conexión por hilo). |
| `test_modelos_restricciones.py` | Campos, unicidad, `PROTECT`/`CASCADE` y `__str__`. |
| `test_migraciones_indices.py` | Migraciones reversibles e índices comprobados en el catálogo de PostgreSQL. |
| `test_admin_secretos.py` | Admin de solo lectura y token oculto en el HTML. |
| `test_reglas_repo.py` | Reglas del repo: sin `float(`, sin `TaskGroup`, sin Cisco, sin Jinja2 y nombres en snake_case. |
| `soporte.py` | Utilidades: reloj controlado, hilos con conexión propia y hash calculado con la fórmula independiente. |

Las pruebas se verificaron con mutaciones: cada regla anterior tiene al menos un test que falla si se rompe.

```powershell
venv\Scripts\python.exe manage.py test apps.providers
venv\Scripts\python.exe -m coverage run --branch --data-file=$env:TEMP\cconor.coverage --source=apps/providers --omit="apps/providers/tests/*" manage.py test apps.providers
venv\Scripts\python.exe -m coverage report --data-file=$env:TEMP\cconor.coverage -m
```

## 8. Preparación para M3: integración de la API de SYSCOM

En el SDD de M1 el proveedor de ejemplo aparece como "Systecom". El proveedor real es **SYSCOM** (developers.syscom.mx).

### 8.1 Qué dice la API (consultado el 2026-10-04)

| Tema | Dato | Fuente |
|---|---|---|
| Acceso | Requiere **cuenta de cliente SYSCOM** y solicitar que se habilite el acceso. Las credenciales son `client_id` y `client_secret`. | [inicio](https://developers.syscom.mx/) |
| URL base | `https://developers.syscom.mx/api/v1` | [/docs](https://developers.syscom.mx/docs) |
| Token | `POST /api/v1/oauth/token` con `application/x-www-form-urlencoded`: `grant_type=client_credentials`, `client_id` y `client_secret`. Es **"válido por un año"**. Este endpoint tiene límites de tasa más estrictos. | [/docs/autenticacion](https://developers.syscom.mx/docs/autenticacion) |
| Uso del token | Cabecera `Authorization: Bearer <token>`. Los productos exigen el scope `ver-productos`. | [/docs](https://developers.syscom.mx/docs) |
| Búsqueda de productos | `GET /productos` con `busqueda`, `categoria`, `marca`, `orden`, `pagina` (1–1000), `limit` (10–1000, por defecto 60), `stock`, `moneda` (`usd`/`mxn`), `iva` e `inventarios`. | [/docs/productos](https://developers.syscom.mx/docs/productos) |
| Detalle de producto | `GET /productos/{id}` (admite hasta 300 ids separados por comas) y `GET /productos?modelo={modelo}`. | [/docs/productos](https://developers.syscom.mx/docs/productos) |
| Moneda | Por defecto **USD**; con `moneda=mxn`, pesos. El tipo de cambio es público en `GET /api/v1/tipocambio` (normal, preferencial y por plazos). | [/docs/productos](https://developers.syscom.mx/docs/productos), [/docs/catalogos](https://developers.syscom.mx/docs/catalogos) |
| Errores | 400; **401** (token inválido, expirado o revocado); **403** (cuenta no habilitada o falta el scope); 404; 405; **429** con `Retry-After`; 500; 501. Cuerpo: `{"error", "code", "values"}`. | [/docs/errores](https://developers.syscom.mx/docs/errores) |
| **Por confirmar** | La documentación no trae ejemplos de respuesta. Faltan la clave del id del producto, los campos de `precios`, las claves de paginación y los campos de la respuesta del token (`expires_in`…). Hay que confirmarlo con la primera llamada real o con su colección de Postman. | — |

### 8.2 ⚠️ Condiciones de uso que afectan al proyecto

El [acuerdo de uso](https://developers.syscom.mx/docs/acuerdo-uso) dice:

> "Queda prohibido el uso de la información del API para alimentar motores de comparación de precios masivos."

> "Se recomienda que los precios competitivos o de distribuidor solo sean visibles para usuarios finales que hayan iniciado sesión."

Además:
- Si se muestran precios al público, aplica una política de precio mínimo anunciado: P1 o P3 con un descuento máximo del 20 %.
- Obliga a "proteger la seguridad de las credenciales de acceso y comunicar inmediatamente cualquier brecha".
- **No dice** si se puede guardar o cachear el catálogo.

El objetivo del cotizador (AGENTS.md: "motor de comparación… precios más baratos") se parece a lo que el acuerdo prohíbe. **Antes de construir M3, CCONOR debe confirmar con SYSCOM, mejor por escrito, que su uso interno está permitido**: cotizaciones propias, sin publicar precios. Esto incluye guardar el catálogo en `RawProviderProduct`.

### 8.3 Cómo encaja M3 con estos modelos

```
.env  ──►  token OAuth2 (client_credentials)  ──►  ProviderToken
                                                       │ Bearer
proveedor "syscom" (Provider) ──► GET /productos …  ◄──┘   (httpx async, timeout_ms)
        │                              │ json.loads(..., parse_float=Decimal)
        └──► por producto: aupsert(proveedor, <id del producto>, producto)
                                       │
                         created / updated / unchanged ──► normalización (catalog)
```

1. **Alta del proveedor:** `Provider(code="syscom", name="SYSCOM", base_url="https://developers.syscom.mx/api/v1")`. Falta decidir si se crea desde el admin o con una migración de datos.
2. **Credenciales:**
   - `PROVIDER_SYSCOM_CLIENT_ID` y `PROVIDER_SYSCOM_CLIENT_SECRET` en `.env`, siguiendo el patrón del SDD §4.3.
   - Añadirlas a `.env_example` sin valores.
   - Nunca en la BD ni en los logs.
3. **Token:**
   - Se pide una vez y se guarda con `ProviderToken.objects.aupdate_or_create(provider=proveedor, defaults={"access_token": …, "expires_at": …, "obtained_at": …})`. Hay una fila por proveedor.
   - Como dura un año, solo se renueva al acercarse `expires_at` o al recibir un 401.
   - **No** se pide un token por petición, porque el endpoint de tokens tiene límites más estrictos.
   - Si dos workers lo renuevan a la vez, gana la última escritura; los dos tokens son válidos.
4. **Productos → `aupsert`:**
   - `external_id`: el id del producto de SYSCOM (la clave exacta está por confirmar). `aupsert` lo convierte a texto.
   - `payload`: el producto tal cual, parseado con `parse_float=Decimal`. Los decimales se guardan como texto (§3).
   - **Parámetros de la consulta siempre iguales.** `moneda`, `iva`, `inventarios`, etc. cambian el contenido de la respuesta. Si varían entre ejecuciones, el hash cambia y todos los productos salen `updated` sin haber cambiado.
   - **Campos volátiles:** todo lo que va en `payload` cuenta para el hash. Hay que decidir si las existencias (stock) van en el payload, y entonces cada cambio de stock es `updated`, o se guardan aparte. Si la respuesta trae marcas de tiempo u otros datos que cambian en cada llamada, conviene excluirlos.
   - La normalización posterior solo necesita reprocesar los productos `created` y `updated`.
5. **Errores**, alineados con el SDD §8:
   - 401: renovar el token y reintentar una vez.
   - 403: problema de cuenta o scope; no reintentar.
   - 429: esperar lo que indique `Retry-After`.
   - 5xx: error del proveedor.
6. **Volumen:** `aupsert` cuesta 1 o 2 sentencias por producto. Con `limit` de hasta 1000 por página va bien. Si se descarga el catálogo completo de forma periódica, conviene valorar un upsert por lotes (aún no existe).
7. **Pruebas de M3:**
   - Sin red, con `httpx.MockTransport` y respuestas de ejemplo guardadas.
   - Con el runner de Django y las convenciones de AGENTS.md §Pruebas.

### 8.4 Preguntas abiertas para el SDD de M3

| # | Pregunta | Quién decide | ¿Bloquea? |
|---|---|---|---|
| 1 | ¿Permite SYSCOM este uso (comparación interna, guardar el catálogo)? (§8.2) | CCONOR con SYSCOM | **Sí** |
| 2 | ¿Hay cuenta habilitada y `client_id`/`client_secret` con el scope `ver-productos`? | CCONOR | **Sí**, para probar contra la API real |
| 3 | ¿Catálogo completo periódico o búsqueda bajo demanda (`busqueda`, `modelo`)? | Equipo | No, pero define el diseño |
| 4 | ¿Pedir en USD y convertir con `/tipocambio` o pedir en MXN? ¿IVA incluido? (SDD P4) | Negocio | No para guardar el crudo; sí para comparar |
| 5 | Estructura real de la respuesta: clave del id, `precios`, paginación | Primera llamada real | No |
| 6 | ¿Las existencias van en el payload o aparte? | Equipo | No |
| 7 | ¿Alta del proveedor por el admin o por migración de datos? | Equipo | No |
| 8 | Los contratos del núcleo del SDD de M1 (`ProviderAdapter`, `get_credentials`, `parse_json`/`parse_price`, `search_all`, errores) **aún no están implementados**: M1a solo hizo los modelos. ¿Se implementan antes de M3 o dentro de M3? **Respondida (2026-10-04): van antes, como módulo propio M2 (contrato y orquestación), ver [13-sdd-m2-contrato-y-orquestacion.md](13-sdd-m2-contrato-y-orquestacion.md).** | Equipo | Sí, para seguir el SDD |

## 9. Trampas conocidas al trabajar con estos modelos

- **ORM async con `asyncio.gather`:** las consultas se ejecutan de una en una en un solo hilo. Para probar concurrencia real hay que usar hilos con conexión propia (`soporte.ejecutar_en_hilos`).
- **Tests async:** `TestCase` no ejecuta `asyncSetUp`; la preparación va en `setUp`/`setUpTestData`. `@unittest.expectedFailure` se pierde en tests `async def`.
- **Dinero:** nunca `float()`; los JSON se parsean con `parse_float=Decimal`.
- **SQL nativo:** `aupsert` depende del nombre real de las columnas y de la restricción única. Si se renombra un campo, hay que tocar también su SQL; las pruebas de §7 lo detectarían.
- **Secretos:** `access_token` nunca va en logs, mensajes de error ni `__str__`.
