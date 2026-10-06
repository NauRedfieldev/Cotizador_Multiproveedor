# 20 · Guía para integrar la API de un proveedor nuevo (estándar de adaptadores)

| | |
|---|---|
| **Para qué sirve** | Ser la referencia y el estándar para conectar cualquier API de proveedor nueva al núcleo de `apps/providers`: qué hay que construir, en qué orden, cómo se prueba, cómo se verifica contra la API real y qué se documenta |
| **Basada en** | La integración de SYSCOM (M3), implementada y verificada contra la API real el 2026-10-05 y comprobada de nuevo el 2026-10-06 (`search_all("camara ip")` → 3 ofertas en `Decimal`, 0 tokens nuevos, 1 GET) |
| **Relación con otros documentos** | **Refina** [14 §8](14-contrato-y-orquestacion.md) ("Cómo añadir un proveedor nuevo"), que da los 5 pasos mínimos, con el proceso completo y las lecciones de M3. **Generaliza** [15](15-guia-m2-para-m3-syscom.md), que era el traspaso específico para SYSCOM. Los casos concretos de SYSCOM están en [16](16-sdd-m3-syscom.md) (diseño y decisiones) y [17](17-adaptador-syscom.md) (funcionamiento) |
| **Fuente de verdad** | El código de [`apps/providers/`](../apps/providers/). Si esta guía y el código discrepan, manda el código; AGENTS.md manda sobre los dos |
| **Fecha** | 2026-10-06 |

## 1. La idea en una página

El núcleo (M2) ya resuelve todo lo que es igual para cualquier proveedor. **Un adaptador solo traduce**: de `ProductQuery` a la petición de su API, y de cada producto de su respuesta a `ProviderOffer`.

| Lo hace el núcleo (no se reprograma) | Lo hace el adaptador |
|---|---|
| Consultar en paralelo a todos los `Provider` activos (`asyncio.gather`) con un `httpx.AsyncClient` compartido | Armar la URL, los parámetros y las cabeceras de **su** API |
| Timeout por proveedor (`Provider.timeout_ms`, cubre token + búsqueda) | Pedir el token a **su** endpoint (`request_token`), si usa OAuth2 |
| Guardar el token en `ProviderToken`, reutilizarlo hasta 5 min antes de caducar, renovarlo con un `Lock` | Distinguir "sin resultados" de "error" según **su** API |
| Ante un 401 con token: invalidarlo y reintentar **una** vez | Encontrar la lista de productos dentro de **su** respuesta |
| Traducir errores HTTP (`raise_for_status`) y de conexión a `ProviderError` | Convertir cada producto en `ProviderOffer` (`parse_offer`) |
| Descartar y contar (`discarded`) las ofertas inválidas sin tumbar al proveedor | Lanzar `ValueError`/`TypeError`/`KeyError`/`ProviderResponseError` cuando un producto no sirve |
| Leer las credenciales de `.env` y nunca registrarlas | Declarar qué credenciales necesita (`required_credentials`) |
| Devolver los errores como datos (`ProviderResult.error`) | — |

```
vista / catalog ──ProductQuery──► search_all ──► registry ──► adapters/<code>.py   ◄── lo único nuevo
               ◄─list[ProviderResult]──┘          │              │ fetch / parse_offer / request_token
                                              tokens.py ◄──► ProviderToken (BD)
                                                                 │ httpx (async)
                                                                 ▼
                                                        API del proveedor
```

**M2 está congelado.** `contracts.py`, `errors.py`, `ProviderAdapter`, `register`, `tokens.py` y `search_all` son contratos. Si un proveedor necesitara cambiarlos (p. ej. un campo nuevo en `ProviderOffer`), se propone y se revisa aparte; nunca se resuelve dentro del adaptador.

## 2. El proceso, fase por fase

Es el mismo orden que funcionó en M3. Cada fase termina en un punto de control con el usuario.

| Fase | Qué se hace | Entregable | Llamadas reales |
|---|---|---|---|
| 0. Precondiciones | Permiso de uso del proveedor, credenciales oficiales, rama `feature/integracion-<code>` | Nada en código | 0 |
| 1. Ficha de la API | Leer la documentación del proveedor y rellenar la ficha de §3 | Borrador del SDD | 0 |
| 2. SDD | Contrato, archivos, casos límite y decisiones (§10) con alternativas y una recomendada | `docs/NN-sdd-mX-<code>.md` aprobado | 0 |
| 3. Reconocimiento real | Confirmar la estructura real de las respuestas con un presupuesto cerrado | Ficha confirmada y muestras saneadas | Presupuesto aprobado (en M3: 1 token + 3 GET + 1 extra) |
| 4. TDD sin red | Muestras, pruebas, adaptador, registro, `.env_example` | Pruebas en verde | 0 |
| 5. Alta y verificación real | `Provider` en el admin y `search_all` contra la API real | Registro de llamadas en el SDD | Presupuesto aprobado (en M3: 2 GET) |
| 6. Cierre | Cobertura, mutaciones, documentación final, `MEMORY.md` | `docs/NN+1-adaptador-<code>.md` | 0 |

`NN` es el siguiente número libre de `docs/`; `mX`, el siguiente módulo libre (consulta `MEMORY.md`: el equipo ya usa M1b y M5a).

### Fase 0. Precondiciones (bloquean la integración)

- **Permiso de uso.** Revisar las condiciones del proveedor. SYSCOM, por ejemplo, prohíbe "alimentar motores de comparación de precios masivos" y pide mostrar los precios de distribuidor solo a usuarios con sesión: CCONOR confirmó que su uso interno está permitido.
- **¿Se puede guardar su catálogo?** Decide si se usa `RawProviderProduct.objects.aupsert()` (§10, P3).
- **Credenciales oficiales** con el alcance (scope) necesario para leer productos. Las proporciona CCONOR y solo van en `.env`.
- **Límites de peticiones** (por token, por IP, por minuto). Si el proveedor no los publica, se asume lo peor: nada en bucle, nada de reintentos ante un 429.
- Sin credenciales solo se avanza hasta la Fase 4, con muestras escritas a mano según la documentación (marcadas como "por confirmar").

## 3. Ficha de la API (rellenar en la Fase 1, confirmar en la Fase 3)

Todo lo que el proveedor no documente va como **"por confirmar"**: no se inventa.

| Pregunta | SYSCOM (ejemplo) |
|---|---|
| URL base (va en `Provider.base_url`) | `https://developers.syscom.mx/api/v1` |
| Formato | JSON. Si fuera XML/SOAP, es una decisión aparte (sin dependencias nuevas) |
| Autenticación | OAuth2 `client_credentials`: `POST /oauth/token`, *form-urlencoded* |
| Vida del token y campo que la indica | `expires_in` = 31536000 (un año) |
| Cómo se envía | `Authorization: Bearer <token>` |
| Endpoint de búsqueda por texto y sus parámetros | `GET /productos?busqueda=…&limit=…` |
| Límites del término y del tamaño de página | `busqueda` ≤ 10 palabras y 120 caracteres; `limit` de 10 a 1000 |
| Búsqueda exacta por modelo/número de parte | `GET /productos?modelo=…` (devuelve **un objeto**, no una lista) |
| Forma de la respuesta | `{"cantidad", "pagina", "paginas", "productos": [...], "todo"}` |
| "Sin resultados" | 200 con `productos: []`; por modelo, 404 `{"error": "product_not_available"}` |
| Identificador del producto | `producto_id` (texto) |
| Nombre, marca, modelo | `titulo`, `marca`, `modelo` |
| ¿El modelo es el número de parte del fabricante? | Sí (D11 de M3) |
| Precios: cuáles hay, cuál paga CCONOR, en qué formato | `precios.{precio_1, precio_especial, precio_descuento, precio_map, precio_lista}`, como texto decimal; CCONOR paga `precio_descuento` |
| Moneda e impuestos: parámetros y valor por defecto | `moneda=usd|mxn`, `iva=0|1` |
| Existencias | `total_existencia` (entero) |
| Códigos de error y su cuerpo | 401 token, 403 cuenta/scope, 429 con `Retry-After`; cuerpo `{"error", "code", "values"}` |

## 4. El contrato del adaptador

```python
@register
class <Code>Adapter(ProviderAdapter):
    code = "<code>"                       # igual a Provider.code ([a-z0-9_-]+)
    required_credentials = (...)          # claves → PROVIDER_<CODE>_<CLAVE> en .env
    uses_token = True | False             # True solo si la API usa un token OAuth2

    async def request_token(self, client) -> NewToken          # solo si uses_token
    async def fetch(self, query, client) -> list[Mapping]      # productos crudos
    def parse_offer(self, item) -> ProviderOffer               # un producto → una oferta
```

| Método | Debe | No debe |
|---|---|---|
| `request_token` | Hacer la petición al endpoint de tokens; traducir el **rechazo de credenciales (400/401) a `ProviderAuthError` sin `status`**; usar `raise_for_status` y `parse_json`; devolver `NewToken(access_token, expires_in)`; si la respuesta no trae esos campos válidos, `ProviderResponseError` sin su contenido | Guardar el token (lo hace `tokens.py`), registrarlo en logs ni incluirlo en mensajes |
| `fetch` | Obtener el token con `await self.token(client)`; llamar a la API con parámetros **fijos y deterministas**; usar `raise_for_status` y `parse_json`; validar la forma de la respuesta (`ProviderResponseError` si no es la esperada); devolver la lista cruda recortada a `query.limit` | Reintentar, poner timeouts, capturar excepciones genéricas, dormir ante un 429, usar `requests` o un cliente síncrono, crear su propio `AsyncClient` |
| `parse_offer` | Construir `ProviderOffer(provider_code=self.code, …, raw=item)`; precio con `parse_price`; `external_id` en texto; moneda ISO en mayúsculas; lanzar `ValueError`/`TypeError`/`KeyError`/`ProviderResponseError` ante un producto inválido | Usar `float`, otro `provider_code`, ni registrar el contenido del producto |

**Por qué `ProviderAuthError` sin `status` en el token.** El núcleo solo reintenta un `ProviderAuthError` con `status == 401`. Si `request_token` lo lanzara con `status=401`, unas credenciales malas gastarían un segundo token del límite del proveedor (D13 de M3).

**Credenciales.** El núcleo entrega `self.credentials` con las claves de `required_credentials` (`{"CLIENT_ID": …, "CLIENT_SECRET": …}`). Si la API las espera con otros nombres (SYSCOM: `client_id` en minúsculas), el adaptador arma el formulario a mano. Si falta alguna, el núcleo devuelve `ProviderConfigError` sin hacer ninguna petición.

## 5. Plantilla de código

Basada en [`adapters/syscom.py`](../apps/providers/adapters/syscom.py). Los `<…>` se sustituyen con los datos de la ficha (§3).

### 5.1 Con OAuth2 (`client_credentials`)

```python
"""Adaptador de la API de <NOMBRE> (SDD <MX>, docs/NN-sdd-mX-<code>.md).

Solo aporta lo específico de <NOMBRE>. El núcleo de M2 ya resuelve el paralelismo, los timeouts,
el token (guardado en ProviderToken y reutilizado), el reintento ante un 401 y el descarte de
ofertas inválidas.
"""
from __future__ import annotations

from typing import Any, Mapping

import httpx

from apps.providers.adapters.base import (
    NewToken,
    ProviderAdapter,
    parse_json,
    parse_price,
    raise_for_status,
)
from apps.providers.contracts import ProductQuery, ProviderOffer
from apps.providers.errors import ProviderAuthError, ProviderResponseError
from apps.providers.registry import register

# Siempre los mismos parámetros: si cambian, el contenido cambia de una consulta a otra.
PARAMETROS_FIJOS = {"<moneda>": "<usd>", "<impuestos>": "<0>"}
MONEDA = "<USD>"  # la que piden PARAMETROS_FIJOS, en ISO 4217
CAMPO_PRECIO = "<campo>"  # el precio que paga CCONOR, confirmado con la API real


@register
class <Code>Adapter(ProviderAdapter):
    code = "<code>"
    required_credentials = ("CLIENT_ID", "CLIENT_SECRET")
    uses_token = True

    @property
    def _base_url(self) -> str:
        return self.provider.base_url.rstrip("/")

    async def request_token(self, client: httpx.AsyncClient) -> NewToken:
        respuesta = await client.post(
            f"{self._base_url}/<ruta-del-token>",
            data={
                "grant_type": "client_credentials",
                "client_id": self.credentials["CLIENT_ID"],
                "client_secret": self.credentials["CLIENT_SECRET"],
            },
        )
        if respuesta.status_code in (400, 401):
            # Sin status: el núcleo no reintenta y no se gasta otro token.
            raise ProviderAuthError(
                self.code, f"<NOMBRE> rechazó las credenciales (HTTP {respuesta.status_code})."
            )
        raise_for_status(self.code, respuesta)
        datos = parse_json(self.code, respuesta)
        try:
            return NewToken(datos["access_token"], datos["expires_in"])
        except (KeyError, TypeError, ValueError):
            raise ProviderResponseError(self.code, "Respuesta de token inválida.") from None

    async def fetch(self, query: ProductQuery, client: httpx.AsyncClient) -> list[Mapping[str, Any]]:
        token = await self.token(client)
        respuesta = await client.get(
            f"{self._base_url}/<ruta-de-productos>",
            params={**self._parametros(query), **PARAMETROS_FIJOS},
            headers={"Authorization": f"Bearer {token}"},
        )
        raise_for_status(self.code, respuesta)
        datos = parse_json(self.code, respuesta)
        productos = datos.get("<lista>") if isinstance(datos, dict) else None
        if not isinstance(productos, list):
            raise ProviderResponseError(self.code, "Respuesta sin lista de productos.")
        return productos[: query.limit]

    def parse_offer(self, item: Mapping[str, Any]) -> ProviderOffer:
        nombre = item["<nombre>"]
        if not isinstance(nombre, str) or not nombre.strip():
            raise ValueError("Producto sin nombre.")
        precio = parse_price(self.code, item["<precios>"][CAMPO_PRECIO])
        if precio == 0:
            raise ValueError("Producto con precio 0.")  # saldría como el más barato
        return ProviderOffer(
            provider_code=self.code,
            external_id=_identificador(item["<id>"]),
            name=nombre.strip(),
            price=precio,
            currency=MONEDA,
            brand=_texto(item.get("<marca>")),
            model=_texto(item.get("<modelo>")),
            mpn=<...>,  # solo si es el número de parte del fabricante (§10, P11)
            stock=_existencias(item.get("<existencias>")),
            raw=item,
        )

    def _parametros(self, query: ProductQuery) -> dict[str, Any]:
        """Traduce ProductQuery a la API respetando sus límites (§6.1)."""
        ...
```

Las funciones `_identificador`, `_texto`, `_existencias` y `_recortar` de `syscom.py` son el patrón de validación de campos. Se copian al módulo nuevo (no se importan de otro adaptador: cada adaptador es independiente). Si un tercer proveedor las necesita idénticas, se propone moverlas a `adapters/base.py` como cambio revisado de M2.

### 5.2 Con clave de API fija (sin OAuth2)

```python
@register
class <Code>Adapter(ProviderAdapter):
    code = "<code>"
    required_credentials = ("API_KEY",)  # → PROVIDER_<CODE>_API_KEY
    # uses_token = False (por defecto): sin request_token y sin fila en ProviderToken.

    async def fetch(self, query, client):
        respuesta = await client.get(
            f"{self._base_url}/<ruta-de-productos>",
            params={**self._parametros(query), **PARAMETROS_FIJOS},
            headers={"<Cabecera-de-la-clave>": self.credentials["API_KEY"]},
        )
        raise_for_status(self.code, respuesta)
        ...
```

Con `uses_token = False`, un 401 llega como `ProviderAuthError(status=401)` **sin reintento**: no hay token que renovar.

### 5.3 Si el token no trae `expires_in`

`NewToken` exige `expires_in` entero y positivo. Si la API no lo devuelve, se usa una constante con la vida documentada por el proveedor (p. ej. `VIDA_DEL_TOKEN = 3600`), anotada en el SDD como decisión. Nunca se pide un token por búsqueda.

### 5.4 Registro

```python
# apps/providers/adapters/__init__.py
from . import syscom  # noqa: F401  (M3, docs/16-sdd-m3-syscom.md)
from . import <modulo>  # noqa: F401  (<MX>, docs/NN-sdd-mX-<code>.md)
```

`ProvidersConfig.ready()` importa este paquete al arrancar, y `@register` deja el adaptador disponible por su `code`.

**Nombre del archivo.** `Provider.code` admite guiones, pero los archivos deben cumplir `^[a-z0-9_]+$` (regla G5): un `code` como `ct-internacional` va en `adapters/ct_internacional.py`, con `code = "ct-internacional"` dentro. Sus variables de `.env` son `PROVIDER_CT_INTERNACIONAL_<CLAVE>`.

## 6. Reglas de traducción

### 6.1 De `ProductQuery` a la petición

| `ProductQuery` | Regla | En SYSCOM |
|---|---|---|
| `term` | Al parámetro de búsqueda por texto, **recortado** a los límites de la API | `busqueda`, ≤ 10 palabras y 120 caracteres |
| `sku` | A la búsqueda exacta, si existe; **manda sobre `term`** si llegan los dos | `modelo` |
| `limit` (1–1000) | Ajustado al rango de la API (pedir el mínimo si se queda corto) y la respuesta **recortada a `query.limit`** | `max(limit, 10)` |
| — | Parámetros fijos: moneda, impuestos y cualquier filtro que cambie el contenido | `moneda=usd`, `iva=0` |

Una consulta **nunca debe fallar** por un límite de formato del proveedor: se ajusta y se recorta. No se envían filtros que oculten productos (p. ej. `stock=true` en SYSCOM).

**Paginación.** Se pide una sola página si el máximo por página del proveedor es ≥ `MAX_LIMIT` (1000). Si es menor, la paginación es una decisión del SDD: varias páginas **secuenciales** dentro de `fetch` (todas cuentan dentro del mismo `timeout_ms`) o quedarse con la primera.

### 6.2 De la respuesta a `ProviderOffer`

| Campo | Regla | Si no se cumple |
|---|---|---|
| `provider_code` | Siempre `self.code` | El núcleo descarta la oferta |
| `external_id` | Id del proveedor en texto (`str`); acepta `str` o `int`, **nunca `bool`** | Se descarta |
| `name` | Texto no vacío, sin espacios alrededor | Se descarta |
| `price` | `parse_price` sobre el precio que paga CCONOR; nunca `float`; el **0 se descarta** | Se descarta |
| `currency` | Constante ISO en mayúsculas, coherente con los parámetros fijos | — |
| `brand`, `model` | Texto sin espacios alrededor | `None` (la oferta se conserva) |
| `mpn` | El número de parte **del fabricante**. Si el proveedor solo tiene su SKU interno, `mpn=None` | `None` |
| `stock` | Entero ≥ 0 (no `bool`); el total, no el desglose | `None` (la oferta se conserva) |
| `raw` | El producto completo, tal cual | — |

Los campos que alimentan el emparejamiento (`brand`, `model`, `mpn`) importan tanto como el precio: `catalog` empareja primero por clave exacta (MPN, o marca + modelo) y solo usa RapidFuzz como apoyo.

### 6.3 "Sin resultados" frente a "error"

Cada API lo señala distinto; el adaptador lo traduce a la regla común: **sin resultados = `[]`** (el resultado sale `ok` con 0 ofertas) y **todo lo demás = error**. Solo se convierte en `[]` la respuesta exacta que la API documenta como "no existe" (en SYSCOM, el 404 con `{"error": "product_not_available"}` y solo en la búsqueda por modelo). Cualquier otro 404, un cuerpo distinto o un cuerpo que no es JSON siguen siendo error.

## 7. Errores: qué hace cada quien

| Situación | Quién la resuelve | Resultado para quien consume |
|---|---|---|
| Faltan credenciales en `.env` | Núcleo (`get_credentials`) | `ProviderConfigError`, sin peticiones |
| Credenciales rechazadas por el endpoint de tokens | **Adaptador** (`ProviderAuthError` sin `status`) | Error, sin reintento |
| 401 en la búsqueda con token | Núcleo: invalida, renueva y reintenta una vez | Normalmente ninguno |
| 403 | `raise_for_status` | `ProviderAuthError(status=403)` |
| 429 | `raise_for_status` | `ProviderRateLimitError(retry_after)`, **sin esperar ni reintentar** |
| Otro 4xx/5xx, JSON inválido | `raise_for_status` / `parse_json` | `ProviderResponseError` |
| Respuesta sin la forma esperada | **Adaptador** | `ProviderResponseError` |
| "Sin resultados" documentado | **Adaptador** (`return []`) | `ok`, 0 ofertas |
| Producto inválido | **Adaptador** lanza; el núcleo descarta | `discarded` + 1 |
| Error de conexión o más de `timeout_ms` | Núcleo | `ProviderResponseError` / `ProviderTimeout` |

Los mensajes de error nunca llevan tokens, credenciales ni el contenido de las respuestas.

## 8. Pruebas (Fase 4)

Runner de Django, **sin red y sin credenciales reales** (AGENTS.md §Pruebas). Se reutiliza `apps/providers/tests/soporte.py`; no hace falta crear utilidades nuevas.

| Utilidad | Para qué |
|---|---|
| `ApiFalsa` | API simulada sobre `httpx.MockTransport`: responde por (host, ruta), consume respuestas en orden y guarda `peticiones` para inspeccionarlas |
| `sin_red()` | Corta cualquier transporte real de httpx y hace fallar la prueba si alguien lo intenta |
| `credenciales_de_prueba("<code>", CLIENT_ID=…)` | Retira de `os.environ` las variables reales del proveedor y pone valores falsos |
| `exigir_bd_de_pruebas()` | Garantiza que se escribe en la BD de pruebas |
| `respuesta_json`, `CENTINELA_TOKEN` | Respuestas rápidas y un token centinela para comprobar que no se filtra |

### 8.1 Archivos

| Archivo | Contenido |
|---|---|
| `apps/providers/tests/test_adaptador_<modulo>.py` | Las pruebas del adaptador, con identificadores propios (en SYSCOM: S1–S27) en el docstring |
| `apps/providers/tests/muestras/<modulo>_<caso>.json` | Respuestas con la **estructura real** y **valores ficticios** (ids `900001…`, marcas "DE MUESTRA", token `token-de-muestra-no-real`). Una por caso: `_token`, `_productos`, `_sin_resultados`, `_modelo`, `_modelo_no_disponible`… |

Las muestras se sirven como texto (`httpx.Response(status, content=bytes)`), no con `json=`, para que los precios pasen por `parse_json` igual que con la API real.

### 8.2 Catálogo mínimo de pruebas

| Bloque | Casos |
|---|---|
| `request_token` (si OAuth2) | 200 → `NewToken` y la petición lleva el formulario exacto · 400 y 401 → `ProviderAuthError` **sin `status`** y sin el secreto en el mensaje · 429 → `retry_after`; 500 → `ProviderResponseError` · respuesta de token inválida → error sin el token · `base_url` con barra final |
| `fetch` | Parámetros enviados (`ApiFalsa.peticiones[i].url.params`) incluidos los fijos, y la cabecera de autenticación con el token **ya guardado** · `sku` manda sobre `term` · recorte del término · `limit` por debajo del mínimo · respuesta sin la forma esperada → error · "sin resultados" → `[]` · cualquier otro 404 → error · `base_url` con barra final |
| `parse_offer` | La muestra produce exactamente la oferta esperada (`Decimal` exacto, moneda, `mpn`, `stock`, `raw`) · cada producto inválido lanza una excepción que el núcleo descarta · marca, modelo y existencias inválidos quedan en `None` y la oferta se conserva |
| Dentro de `search_all` | Búsqueda completa con descartes contados · el token se reutiliza (un solo POST en dos búsquedas) · 401 → renueva y reintenta · 403 sin reintento · 429 con `retry_after` · credenciales rechazadas → **un solo** POST de token · sin credenciales → ninguna petición · "sin resultados" → `ok` con 0 ofertas · ni el token ni el secreto aparecen en logs ni mensajes (`assertLogs` + centinelas) |
| Registro | El `code` está en `registered_codes()` con su clase, `required_credentials` y `uses_token` |

### 8.3 Esqueleto

```python
@tag("django_db")
class <Code>EnSearchAllTests(TestCase):
    def setUp(self):
        exigir_bd_de_pruebas()
        self.enterContext(sin_red())
        self.enterContext(credenciales_de_prueba("<code>", CLIENT_ID="id-de-prueba", CLIENT_SECRET=CENTINELA_SECRETO))
        self.api = ApiFalsa()
        self.proveedor = Provider.objects.create(code="<code>", name="<NOMBRE>", base_url=BASE_URL)

    async def test_una_busqueda_completa_devuelve_ofertas_y_cuenta_los_descartes(self):
        """X1 Pide el token, lo guarda, busca, convierte y descarta el producto inválido."""
        # Arrange
        self.api.responder(HOST, RUTA_TOKEN, respuesta_muestra("<modulo>_token"))
        self.api.responder(HOST, RUTA_PRODUCTOS, respuesta_muestra("<modulo>_productos"))

        # Act
        [resultado] = await search_all(ProductQuery(term="camara ip"), transport=self.api.transport)

        # Assert
        ...
```

Recordatorios del runner: `TestCase` no ejecuta `asyncSetUp` (la preparación va en `setUp`); un bug conocido se marca con `@unittest.expectedFailure` en una prueba **síncrona** y `HALLAZGO-n` en el docstring.

## 9. Verificación contra la API real (Fases 3 y 5)

Son **credenciales de producción con límites de peticiones** (AGENTS.md §Límites).

**Protocolo:**
1. Presupuesto **aprobado por el usuario antes** de la primera llamada: cuántos tokens y cuántos GET, y para qué.
2. Llamadas en secuencia, con pausas, parando ante el primer 401, 403, 429 o 5xx.
3. **Un solo token** en toda la integración: lo pide y lo guarda el núcleo; después se reutiliza.
4. Nunca se imprimen ni se guardan en el repositorio tokens, credenciales ni precios reales. Las respuestas crudas se analizan fuera del repo y se publican solo claves, tipos, recuentos y relaciones (p. ej. el orden de los precios).
5. Cada llamada se anota en el registro del SDD (fecha, fase, petición, resultado) y en `MEMORY.md`.

**Antes de la verificación real**, dar de alta el proveedor en el admin (**Providers → Add**): `code`, `name`, `base_url`, `timeout_ms` = 8000 y `active` solo cuando ya haya credenciales en `.env`.

**Verificación de la Fase 5** (la misma que se repitió el 2026-10-06): un script fuera del repo, en el scratchpad, que pasa por `search_all` y muestra solo las ofertas normalizadas.

```python
import asyncio

from apps.providers.contracts import ProductQuery
from apps.providers.service import search_all

resultados = asyncio.run(search_all(ProductQuery(term="camara ip", limit=3)))
for r in resultados:
    print(f"Proveedor: {r.provider_code} | ok={r.ok} | {r.elapsed_ms} ms | descartadas={r.discarded}")
    if r.error:
        print("  Error:", type(r.error).__name__, r.error)
    for o in r.offers:
        print(f"  - [{o.external_id}] {o.name}")
        print(f"    marca={o.brand} modelo={o.model} precio={o.price!r} {o.currency} stock={o.stock}")
```

Se ejecuta desde Git Bash con `venv/Scripts/python.exe manage.py shell -c "exec(open(r'<ruta>', encoding='utf-8').read())"`. Ojo: `search_all` consulta **todos** los `Provider` activos; con varios proveedores, cada uno gasta una petición.

**Explorar a mano con Thunder Client** (opcional, cuenta dentro del presupuesto): el token guardado se copia al portapapeles sin mostrarlo y se pega en una variable de entorno de Thunder Client, nunca en una colección guardada dentro del repo.

```powershell
venv\Scripts\python.exe manage.py shell --no-imports -v 0 -c "from apps.providers.models import ProviderToken as T; print(T.objects.get(provider__code='<code>').access_token, end='')" | clip
```

## 10. Decisiones estándar para el SDD

Cada proveedor nuevo responde estas preguntas en su SDD, con alternativas y una recomendada. La recomendada por defecto es la que se tomó en M3 ([16 §12](16-sdd-m3-syscom.md)); un proveedor puede apartarse si su API lo exige, explicándolo.

| # | Pregunta | Recomendada por defecto | Por qué |
|---|---|---|---|
| P1 | Modo de consulta | Solo búsqueda bajo demanda con `search_all` | Es lo que soporta el contrato; descargar el catálogo exige permiso, tareas programadas y upsert por lotes |
| P2 | Moneda e impuestos | Pedir siempre la misma moneda, **sin impuestos**, y declararla en `currency` | `quotes` convierte con un tipo de cambio explícito y congelado ([19](19-propuesta-conversion-usd-mxn.md)) y aplica `tax_rate` una sola vez |
| P3 | Guardar el crudo (`aupsert`) | No, hasta tener el permiso del proveedor | Condiciones de uso |
| P4 | Búsqueda por `sku` | Búsqueda exacta, si la API la tiene; manda sobre `term` | Encaja con el emparejamiento por clave exacta |
| P5 | Límites de formato (término, `limit`) | Ajustar y recortar | La consulta nunca falla por un detalle de formato |
| P6 | Paginación | Una página si el máximo ≥ 1000 | Ver §6.1 |
| P7 | Existencias | Total en `stock`; desglose solo en `raw`; sin filtrar por stock | El cotizador puede preferir a quien tiene existencias |
| P8 | Alta del `Provider` | Manual en el admin | Una migración de datos rompería por unicidad `test_supplier_provider.py` |
| P9 | Muestras de prueba | Estructura real y valores ficticios | Pruebas fieles sin datos reales en el repo |
| P10 | Qué precio | El que paga CCONOR (distribuidor/descuento), nunca el público; el 0 se descarta | Es el coste real de la cotización |
| P11 | `model` y `mpn` | `mpn` solo si es el número de parte del fabricante | Un SKU interno en `mpn` provocaría emparejamientos falsos |
| P12 | Credenciales rechazadas en el token | `ProviderAuthError` sin `status` | No gastar un segundo token |
| P13 | Qué es "sin resultados" | Solo la respuesta documentada por la API; lo demás es error | §6.3 |
| P14 | Variables de `.env` | `PROVIDER_<CODE>_<CLAVE>`; solo los nombres en `.env_example` | AGENTS.md |
| P15 | Llamadas reales | Las hace el agente con presupuesto cerrado y registro | §9 |
| P16 | Enlace con `Supplier` | Fuera de la integración: lo decide el equipo ([17 §6.3](17-adaptador-syscom.md)) | `Supplier` aún no decide si guarda distribuidores o marcas |

## 11. Archivos que crea o toca una integración

| Crea | Toca | No toca |
|---|---|---|
| `apps/providers/adapters/<modulo>.py` | `apps/providers/adapters/__init__.py` (el import) | `contracts.py`, `errors.py`, `adapters/base.py`, `registry.py`, `tokens.py`, `service.py` (M2 congelado) |
| `apps/providers/tests/test_adaptador_<modulo>.py` | `.env_example` (solo los nombres de las variables) | Otros adaptadores, `apps/catalog/**`, `apps/quotes/**` |
| `apps/providers/tests/muestras/<modulo>_*.json` | `.env` local (valores reales, nunca en git) | `settings/**`, `requirements*.txt` (nada de dependencias nuevas) |
| `docs/NN-sdd-mX-<code>.md` y `docs/NN+1-adaptador-<code>.md` | `MEMORY.md`; `AGENTS.md` (§Arquitectura: el adaptador nuevo) | Ninguna migración: no hay cambios de modelo |

## 12. Criterio de aceptación

| Comprobación | Esperado |
|---|---|
| `python manage.py check` | Sin errores; el `code` aparece en `registered_codes()` |
| `python manage.py makemigrations --check --dry-run` | Sin cambios |
| `python manage.py test` | Todo en OK, incluidas las reglas G1–G9 (sin `float(`, sin `TaskGroup`, snake_case, `providers` no importa de `catalog`/`quotes`…) |
| Cobertura del adaptador | 100 % de líneas y ramas (comandos en AGENTS.md §Pruebas) |
| Mutaciones | Cada regla de §4, §6 y cada decisión del SDD tiene una prueba que falla si se rompe |
| Verificación real | `search_all` devuelve ofertas del proveedor en `Decimal`, con los campos confirmados y 0 tokens extra |
| Documentación | SDD con el registro de llamadas reales y documento final de funcionamiento y conexiones |

## 13. Errores a evitar (aprendidos en M3)

- **`response.json()` de httpx devuelve `float`.** Siempre `parse_json` (`parse_float=Decimal`) y `parse_price`.
- **Los precios pueden llegar como texto** (`"85.50"`): `parse_price` los acepta; nunca `float(...)`.
- **Un precio 0 es válido para la API, pero no para el comparador:** se descarta.
- **Ids numéricos:** `str()` siempre, y rechazar `bool` (en Python `True` es un `int`).
- **`base_url` con barra final** produce `//productos`: usar `rstrip("/")`.
- **La búsqueda exacta puede devolver un objeto y no una lista:** envolverlo en una lista.
- **No confundir "sin resultados" con error** (§6.3), ni al revés.
- **No poner timeouts en el adaptador:** el `AsyncClient` del núcleo va con `timeout=None` y manda `Provider.timeout_ms`.
- **No pedir tokens a mano, en bucle ni por búsqueda, ni invalidar un token real.**
- **Las existencias pueden venir topadas** (SYSCOM solo mostró 200 y 500): tratarlas como indicativas.
- **No ejecutar `python` a secas en Git Bash** (alias de la Microsoft Store): usar `venv/Scripts/python.exe`.
- **Pasar un script a `manage.py shell` por una tubería de PowerShell añade un BOM** y falla con `U+FEFF`: usar `-c "exec(open(...).read())"`.
- **Mutaciones:** restaurar los bytes desde memoria, nunca con `git restore` (`core.autocrlf` cambia LF↔CRLF y puede borrar cambios sin commit).
- **Entre módulos de prueba** importar solo funciones y constantes, nunca clases `TestCase` (se ejecutarían dos veces).

## 14. Plantilla de prompt para arrancar una integración

```text
Integra la API de <NOMBRE> (code "<code>") en el Cotizador CCONOR siguiendo el estándar
de docs/20-guia-integrar-api-de-proveedor.md.

Lee antes: AGENTS.md (manda sobre todo), MEMORY.md, docs/20, docs/14 y, como ejemplo
completo, docs/16 y docs/17 junto con apps/providers/adapters/syscom.py.
Documentación de la API del proveedor: <enlace o archivo>.

Antes de escribir código:
1.- Rellena la ficha de la API (docs/20 §3); lo no documentado va "por confirmar".
2.- Propón el SDD (docs/NN-sdd-mX-<code>.md) con las decisiones P1–P16 de docs/20 §10,
    cada una con alternativas y una recomendada.
3.- Propón el presupuesto de llamadas reales para el reconocimiento (Fase 3).
No se modifica nada hasta tener el plan aprobado. La última tarea es documentar el
adaptador en docs/NN+1-adaptador-<code>.md y actualizar MEMORY.md.
```
