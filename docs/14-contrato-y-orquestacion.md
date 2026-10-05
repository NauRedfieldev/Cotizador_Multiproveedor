# 14 · Contrato y orquestación de proveedores (M2): cómo funciona y cómo se conecta

| | |
|---|---|
| **Fuente de verdad** | El código de [`apps/providers/`](../apps/providers/): `contracts.py`, `errors.py`, `adapters/base.py`, `registry.py`, `tokens.py` y `service.py`. Si este documento y el código discrepan, manda el código. |
| **Diseño** | [13-sdd-m2-contrato-y-orquestacion.md](13-sdd-m2-contrato-y-orquestacion.md). Las dudas de §12 se decidieron: todas en la alternativa A. |
| **Estado** | Implementado el 2026-10-05. 86 pruebas, con 100 % de líneas y ramas en los módulos de M2, verificadas con 21 mutaciones. |
| **Para construir M3** | [15-guia-m2-para-m3-syscom.md](15-guia-m2-para-m3-syscom.md): archivos clave de M2, contrato del adaptador, API de SYSCOM traducida al contrato, decisiones D0–D10 y prompt para generar el SDD de M3. |

## 1. Para qué sirve

M2 es la única puerta de entrada a las APIs de los proveedores. Con una sola llamada, `search_all`, el resto del sistema:
- consulta en paralelo a todos los proveedores activos;
- recibe sus productos convertidos a un formato común (`ProviderOffer`, con precios en `Decimal`);
- recibe cada error como un dato, sin que un proveedor caído o lento afecte a los demás.

Lo específico de cada proveedor (URL, autenticación, formato de respuesta) queda encerrado en su **adaptador**.

## 2. Piezas

| Archivo | Responsabilidad | Lo que expone |
|---|---|---|
| `contracts.py` | Datos que entran y salen | `ProductQuery(term, sku, limit=60)` · `ProviderOffer` · `ProviderResult` |
| `errors.py` | Errores comunes | `ProviderError` y sus hijos: `ProviderTimeout`, `ProviderAuthError(status)`, `ProviderRateLimitError(retry_after)`, `ProviderResponseError`, `ProviderConfigError` |
| `adapters/base.py` | Lo que tiene que implementar cada proveedor | `ProviderAdapter` (`fetch`, `parse_offer`, `request_token`, `token`) · `NewToken` · auxiliares `get_credentials`, `parse_json`, `parse_price`, `raise_for_status` |
| `adapters/__init__.py` | Lista de adaptadores reales | Importa cada `adapters/<code>.py` (vacío hasta M3) |
| `registry.py` | Qué adaptador atiende a cada `Provider.code` | `@register`, `get_adapter_class`, `registered_codes` |
| `tokens.py` | Token OAuth2 guardado en BD | `get_token`, `invalidate_token`, `RENEWAL_MARGIN` (5 min) |
| `service.py` | Orquestación | `search_all(query, *, transport=None)` |
| `apps.py` | Arranque | `ready()` importa `adapters` para que se registren |

## 3. Cómo funciona una búsqueda

```python
from apps.providers.contracts import ProductQuery
from apps.providers.service import search_all

resultados = await search_all(ProductQuery(term="cámara ip", limit=20))
for resultado in resultados:              # uno por proveedor activo, ordenados por code
    if resultado.ok:
        for oferta in resultado.offers:   # ProviderOffer, precio Decimal
            ...
    else:
        ...                               # resultado.error: ProviderTimeout, ProviderAuthError…
```

Paso a paso (`service.py`):

```
search_all(query)
 1. Provider.objects.filter(active=True).order_by("code")   (ORM async)
    └─ ninguno → []                                          (no se abre ninguna conexión)
 2. httpx.AsyncClient(timeout=None)                          (uno compartido; manda timeout_ms)
 3. asyncio.gather(_query_provider(p) para cada proveedor, return_exceptions=True)
       _query_provider(p)
         ├─ registry.get_adapter_class(p.code)       → sin adaptador: ProviderConfigError
         ├─ get_credentials(p.code, …)               → faltan: ProviderConfigError (sin petición)
         ├─ wait_for(_fetch_with_auth_retry, p.timeout_ms)
         │     adapter.fetch(query, client)          → elementos crudos de la API
         │     401 con token → invalidate_token + un reintento
         ├─ _parse_offers: adapter.parse_offer(item) para cada elemento
         │     inválido → se descarta, discarded += 1, aviso en el log (sin contenido)
         └─ ProviderResult(code, offers, error, elapsed_ms, discarded)
 4. lista de ProviderResult en orden de code
```

- **El timeout es por proveedor** (`Provider.timeout_ms`, 8000 por defecto, editable en el admin). Como los proveedores van en paralelo, una búsqueda tarda aproximadamente lo que el más lento, con ese tope.
- **El cliente HTTP no tiene timeout propio:** el de httpx (5 s) cortaría antes que el de 8 s.
- **`search_all` no guarda nada en la BD**, salvo el token (§4). Guardar el crudo está pendiente de que SYSCOM confirme el uso.

## 4. Token OAuth2

Para los proveedores con `uses_token = True`:

```
adapter.fetch → await self.token(client) → tokens.get_token(provider, request)
   ProviderToken vigente (vence después de ahora + 5 min) → se reutiliza: 0 peticiones
   caducado o inexistente → Lock(bucle, proveedor) → se vuelve a mirar → request_token()
       → ProviderToken.aupdate_or_create(access_token, expires_at, obtained_at)
401 en fetch → invalidate_token (expires_at = ahora; la fila no se borra) → un único reintento
403 → no se reintenta (problema de cuenta o de scope)
```

- **Concurrencia:** dos búsquedas simultáneas en el mismo proceso piden **un solo** token, gracias al Lock. Entre workers distintos gana la última escritura y los dos tokens son válidos.
- **Secretos:** el token nunca aparece en logs ni en mensajes, y `NewToken` lo oculta en su `repr`. Las credenciales salen de `.env` y nunca de la BD.

## 5. Errores y tiempos: qué recibe quien consume

| `resultado.error` | Cuándo | Qué conviene hacer |
|---|---|---|
| `None` (`ok`) | El proveedor respondió | Usar `offers`; `discarded` dice cuántas ofertas se descartaron por inválidas |
| `ProviderTimeout` | Superó `timeout_ms`, o httpx agotó su tiempo | Mostrar "sin respuesta"; subir `timeout_ms` si se repite |
| `ProviderAuthError(status)` | 401 tras renovar el token, o 403 | Revisar credenciales (401) o la cuenta y el scope (403) |
| `ProviderRateLimitError(retry_after)` | 429 | No reintentar en la búsqueda en vivo; esperar `retry_after` segundos |
| `ProviderResponseError` | Otro 4xx/5xx, JSON inválido, error de conexión o excepción inesperada (que se registra con traza) | Mostrar el error; revisar el log si es inesperado |
| `ProviderConfigError` | Sin adaptador, sin credenciales, o `uses_token` sin `request_token` | Error de configuración: alta del proveedor o `.env` |

Los mensajes son fijos y legibles, por ejemplo `"[syscom] Sin respuesta en 8000 ms."`. La lista exacta está en el SDD §8.

## 6. Cómo se conecta con los demás componentes

```
                      vistas async (quotes)         catalog (emparejamiento, equipo)
                               │ ProductQuery                    ▲ ProviderOffer
                               ▼                                 │
  M1a: Provider ──config──► service.search_all ──────────────────┘
  M1a: ProviderToken ◄────► tokens.py                 ▲
                            registry ──► adapters/<code>.py (M3: syscom)
                                                       │ httpx
                                                       ▼
                                               API del proveedor
```

### 6.1 Con M1a (modelos de `apps/providers`)
- **`Provider`** es la configuración. Solo se consultan los que tienen `active=True`. `code` elige el adaptador; `base_url` y `timeout_ms` los usa la consulta. Se dan de alta en el admin.
- **`ProviderToken`** es el almacén del token: `tokens.py` lo lee, lo crea o lo marca como caducado. Nunca se borra.
- **`RawProviderProduct.aupsert`** no lo usa M2 (decisión 2 del SDD). Cuando se apruebe guardar el crudo, bastará con `await RawProviderProduct.objects.aupsert(provider, oferta.external_id, oferta.raw)` por cada oferta, porque `ProviderOffer` ya trae `external_id` y `raw`. Ver [02 §4](02-modelo-de-datos.md).

### 6.2 Con M3 (adaptador de SYSCOM)
Un adaptador es un archivo `apps/providers/adapters/syscom.py`. Este esbozo **no está implementado**. Usa los datos verificados de la API ([02 §8.1](02-modelo-de-datos.md)); los campos de la respuesta están **por confirmar**:

```python
@register
class SyscomAdapter(ProviderAdapter):
    code = "syscom"                                        # = Provider.code
    required_credentials = ("CLIENT_ID", "CLIENT_SECRET")  # PROVIDER_SYSCOM_CLIENT_ID / _SECRET
    uses_token = True                                      # OAuth2 client_credentials, dura 1 año

    async def request_token(self, client):
        r = await client.post(f"{self.provider.base_url}/oauth/token", data={
            "grant_type": "client_credentials",
            "client_id": self.credentials["CLIENT_ID"],
            "client_secret": self.credentials["CLIENT_SECRET"]})
        if r.status_code in (400, 401):
            raise ProviderAuthError(self.code, "Credenciales rechazadas.", status=r.status_code)
        raise_for_status(self.code, r)
        datos = parse_json(self.code, r)
        return NewToken(datos["access_token"], int(datos["expires_in"]))   # claves por confirmar

    async def fetch(self, query, client):
        r = await client.get(f"{self.provider.base_url}/productos",
                             params={"busqueda": query.term, "limit": query.limit},
                             headers={"Authorization": f"Bearer {await self.token(client)}"})
        raise_for_status(self.code, r)
        return parse_json(self.code, r)["productos"]                       # clave por confirmar

    def parse_offer(self, item):                                           # campos por confirmar
        return ProviderOffer(provider_code=self.code, external_id=str(item["id"]),
                             name=item["titulo"], price=parse_price(self.code, item["precio"]),
                             currency="USD", brand=item.get("marca"), model=item.get("modelo"),
                             raw=item)
```

Después hay que:
1. Añadir `from . import syscom  # noqa: F401` a `adapters/__init__.py`.
2. Dar de alta `Provider(code="syscom", base_url="https://developers.syscom.mx/api/v1")`.
3. Poner las dos variables en `.env`.

Las peticiones deben enviar **siempre los mismos parámetros** (`moneda`, `iva`…), porque si no, el contenido y el hash cambian de una consulta a otra ([02 §8.3](02-modelo-de-datos.md)).

### 6.3 Con `catalog` (emparejamiento, equipo)
`ProviderOffer` trae lo que usa el emparejamiento determinista de `catalog/constants.py`:

| Método de `catalog` | Campos de la oferta |
|---|---|
| `DETERMINISTICO_MPN` (MPN exacto) | `mpn` |
| `DETERMINISTICO_MODELO` (marca y modelo exactos) | `brand` + `model` |
| `DETERMINISTICO_SKU` (SKU de fabricante) | `mpn` o `model`, según lo que dé cada proveedor |

`catalog` puede normalizarlos con `normalizar_clave()` (`apps/catalog/normalizer.py`). `providers` nunca importa `catalog`: la dirección es `quotes → catalog → providers`.

**De la oferta a su distribuidor** (duda 7 del SDD, alternativa A, 2026-10-05). `catalog.Supplier` tiene un vínculo opcional y uno a uno con `providers.Provider` (`Supplier.provider`, con PROTECT). Así, el distribuidor de una oferta se obtiene sin comparar textos:

```python
supplier = await Supplier.objects.aget(provider__code=oferta.provider_code)
```

Un `Supplier` sin API (cotización manual) deja `provider` vacío. Sigue abierto, para el equipo, si `Supplier` guarda solo distribuidores o también marcas (la marca de una oferta va en `oferta.brand`).

### 6.4 Con `quotes`
- `oferta.price` es `Decimal`, igual que `Product.unit_price` y `QuoteItem.unit_price` (2 decimales en BD; `quotes` redondea con `ROUND_HALF_UP`).
- `oferta.currency` dice la moneda. SYSCOM da USD por defecto, y la conversión a MXN y el IVA (`Quote.tax_rate`, 0.16 por defecto) los decide `quotes`. **Pendiente** (P4 del SDD de M1).

### 6.5 Con las vistas async
`search_all` es `async`: se llama con `await` desde una vista `async def` servida por ASGI (uvicorn). No hay que usar `sync_to_async` ni hilos. Las vistas reciben los errores como datos y deciden qué mostrar.

## 7. Cómo probarlo

```powershell
venv\Scripts\python.exe manage.py test apps.providers              # todo providers
venv\Scripts\python.exe manage.py test apps.providers.tests.test_service
venv\Scripts\python.exe manage.py test apps.providers --tag unit   # solo las que no usan BD
```

| Archivo de pruebas | Qué cubre |
|---|---|
| `test_contracts.py` | Validaciones de `ProductQuery`, `ProviderOffer` y `ProviderResult` |
| `test_helpers_adaptador.py` | Errores, `get_credentials`, `parse_json`, `parse_price`, `raise_for_status`, `NewToken` |
| `test_registry.py` | `@register` y la carga de adaptadores al arrancar |
| `test_tokens.py` | Reutilización, margen, invalidación y concurrencia del token |
| `test_service.py` | `search_all` completo: paralelismo, timeouts, 401/403/429/5xx, descartes, cancelación, secretos en logs |

Las utilidades de `tests/soporte.py` permiten probar sin red:
- `AdaptadorDePrueba` y `crear_adaptador(code, …)`: un adaptador realista de una API JSON ficticia;
- `ApiFalsa`: un `httpx.MockTransport` que responde por host y ruta y guarda las peticiones;
- `adaptadores_registrados(...)`: registra adaptadores solo durante la prueba.

Un adaptador real se prueba igual, con respuestas de ejemplo de su API en una `ApiFalsa`.

## 8. Cómo añadir un proveedor nuevo

1. Crear `apps/providers/adapters/<code>.py` con una clase que herede de `ProviderAdapter`:
   - `code`, que es igual a `Provider.code`;
   - `fetch`, que usa `raise_for_status` y `parse_json`;
   - `parse_offer`, que usa `parse_price`, monedas ISO en mayúsculas y `raw=item`;
   - si usa OAuth2, `required_credentials`, `uses_token = True` y `request_token`.
2. Decorarla con `@register` e importarla en `adapters/__init__.py`.
3. Añadir sus credenciales a `.env` (`PROVIDER_<CODE>_<CLAVE>`) y sus nombres, sin valores, a `.env_example`.
4. Dar de alta el `Provider` en el admin, con `base_url` y `timeout_ms`.
5. Escribir sus pruebas con `ApiFalsa`, sin red, y documentar el adaptador en `docs/` (AGENTS.md §Forma de trabajar).

## 9. Límites conocidos

- **No se guarda el crudo** (`RawProviderProduct`) hasta que SYSCOM confirme el uso ([02 §8.2](02-modelo-de-datos.md)).
- **Un 429 no se reintenta:** la búsqueda en vivo prefiere responder con lo que tenga.
- **El Lock del token es por proceso:** con varios workers puede pedirse un token duplicado de forma excepcional.
- **Pendientes del equipo y del negocio:**
  - la moneda y el IVA;
  - si `Supplier` guarda solo distribuidores o también marcas. El vínculo `Supplier`/`Provider` ya existe (alternativa A).
