# 33 · Guía para integrar un proveedor sin API por web scraping (estándar de scrapers)

| | |
|---|---|
| **Para qué sirve** | Ser la referencia y el estándar para conectar la tienda web de un proveedor **sin API** al cotizador: qué construir, en qué orden, cómo reconocer el sitio sin riesgos, cómo se prueba, cómo se verifica contra el sitio real y qué se documenta |
| **Basada en** | La integración de Exel del Norte (M9), implementada y verificada contra el sitio real el 2026-10-27: `search_all("camara ip", limit=3)` → 3 ofertas en `Decimal` MXN, una sola sesión para dos búsquedas |
| **Relación con otros documentos** | **Adapta** [20](20-guia-integrar-api-de-proveedor.md) (el estándar para APIs) a sitios web: mismas fases, mismo contrato y mismas reglas de pruebas y llamadas reales. Lo que aquí no se repite vale tal cual desde docs/20. El caso completo de Exel está en [31](31-sdd-m9-exel-web.md) (diseño, decisiones y registro de peticiones) y [32](32-scraper-exel-web.md) (funcionamiento) |
| **Fuente de verdad** | El código de [`apps/providers_web/`](../apps/providers_web/). Si esta guía y el código discrepan, manda el código; AGENTS.md manda sobre los dos |
| **Fecha** | 2026-10-27 |

## 1. La idea en una página

Un scraper es **un adaptador más**: hereda de `ProviderAdapter` (a través de `WebScraperAdapter`), se registra con el mismo `@register` y `search_all` lo consulta igual que a SYSCOM. El núcleo de M2 sigue resolviendo el paralelismo, el timeout, el descarte de ofertas inválidas, los errores como datos y la sesión, porque la sesión del sitio se guarda **como si fuera un token**.

| Lo hace el núcleo (no se reprograma) | Lo hace el scraper |
|---|---|
| Consultar en paralelo a los `Provider` activos con un `httpx.AsyncClient` compartido | Iniciar sesión en el sitio (`request_token`) y devolver las cookies como token |
| Timeout por proveedor (`Provider.timeout_ms`, cubre login + búsqueda + precios) | Pedir la búsqueda y, si el sitio los separa, los precios |
| Guardar la sesión en `ProviderToken`, reutilizarla y renovarla 5 min antes de caducar | Detectar una sesión caducada y lanzar `ProviderAuthError(status=401)` |
| Ante un 401: invalidar la sesión, entrar de nuevo y reintentar **una** vez | Distinguir «sin resultados» de «el sitio cambió» |
| Leer las credenciales de `.env` y nunca registrarlas | Quedarse solo con los campos permitidos del crudo (sin datos personales) |
| Descartar y contar las ofertas inválidas | Convertir cada producto en `ProviderOffer` (`parse_offer`) |

```
vista ──ProductQuery──► search_all ──► registry ──► apps/providers/adapters/<code>.py      (API)
     ◄─list[ProviderResult]─┘            │      └─► apps/providers_web/scrapers/<code>.py   (web)  ◄── lo único nuevo
                                     tokens.py ◄──► ProviderToken (la sesión del sitio)
```

**Dependencias en un solo sentido: `providers_web → providers`** (regla G10). Un scraper nunca importa de `catalog` ni de `quotes` (G4), y `apps/providers` nunca importa de `apps/providers_web`. **M2 sigue congelado**: si un sitio parece exigir cambiarlo, se propone y se revisa aparte.

## 2. ¿API o web scraping?

| Pregunta | Si la respuesta es sí… |
|---|---|
| ¿El proveedor da una API documentada con credenciales? | API: docs/20 y `apps/providers/adapters/`. **Siempre se prefiere** |
| ¿No hay API, pero CCONOR tiene acceso a su tienda web y el uso automatizado está permitido? | Web scraping: esta guía y `apps/providers_web/scrapers/` |
| ¿La tienda solo funciona con JavaScript y no tiene servicios JSON ni HTML con los datos? | **Parar.** Sin navegadores sin interfaz (Playwright, Selenium): son dependencias prohibidas (AGENTS.md) |
| ¿Hay captcha, desafío antibots o bloqueo activo? | **Parar.** Nunca se evade; se habla con el proveedor |

## 3. El proceso, fase por fase

El mismo orden que en docs/20, con lo específico de un sitio web. Cada fase termina en un punto de control con el usuario.

| Fase | Qué se hace | Entregable | Peticiones reales |
|---|---|---|---|
| 0. Precondiciones | Permiso de uso automatizado, cuenta de cliente, URL de la tienda, rama `feature/…` | Nada en código | 0 |
| 1. Ficha del sitio | Rellenar la ficha de §4 con lo que se sabe; lo demás, «por confirmar» | Borrador del SDD | 0 |
| 2. SDD y esqueleto | Decisiones P1–P16 (docs/20 §10) y W1–W12 (§11), cada una con alternativas y una recomendada | `docs/NN-sdd-mX-<code>-web.md` | 0 |
| 3. Reconocimiento real | Confirmar la estructura del sitio con un presupuesto cerrado (§5) | Ficha confirmada y respuestas crudas **fuera del repo** | Presupuesto aprobado (Exel: 21 peticiones HTTP en varias tandas) |
| 4. TDD sin red | Muestras saneadas, pruebas, scraper, registro, `.env_example`, configuración | Pruebas en verde, 100 % de cobertura, mutaciones | 0 |
| 5. Alta y verificación real | `Provider` en el admin (o en `manage.py shell` con autorización) y `search_all` real | Registro de peticiones en el SDD | Presupuesto aprobado (Exel: 6 + 2 de SYSCOM) |
| 6. Cierre | Documento final, `docs/30`, `MEMORY.md`, propuesta a `AGENTS.md` si cambia una regla | `docs/NN+1-scraper-<code>-web.md` | 0 |

### Fase 0. Precondiciones (bloquean la integración)

- **Permiso de uso automatizado**, confirmado por CCONOR. Exel: confirmado por el usuario el 2026-10-27.
- **Cuenta de cliente** de CCONOR en la tienda, si los precios lo exigen. Sus credenciales van **solo en `.env`**, como `PROVIDER_<CODE>_USUARIO` y `PROVIDER_<CODE>_CONTRASENA`.
- **Comprobar el login a mano en el navegador antes del primer intento automático.** Con Exel se gastaron 2 logins rechazados por un usuario y una contraseña mal escritos en `.env`.
- **URL de la tienda**, que irá en `Provider.base_url`.
- **`robots.txt`**: es la primera petición del reconocimiento. Exel solo prohíbe `/Contenido/*`.

## 4. Ficha del sitio (rellenar en la Fase 1, confirmar en la Fase 3)

| Pregunta | Exel del Norte (ejemplo) |
|---|---|
| URL base (`Provider.base_url`) | `https://www.exel.com.mx/xlstore/` |
| Plataforma | ASP.NET WebForms (`__VIEWSTATE`, `__EVENTVALIDATION`); servidor `volt-adc` |
| Codificación | UTF-8 |
| `robots.txt` | `Disallow: /Contenido/*` (la tienda, permitida) |
| ¿Exige sesión? | Toda la tienda: `GET /xlstore/` → 302 a `/xlstore/Acceso` |
| Formulario de login | `POST /xlstore/Acceso` con los ocultos de `Form1` + `ctl00$MainContent$txtUsuario`, `ctl00$MainContent$txtPassword` y el botón `ctl00$MainContent$L_ButtonLogin` |
| Login correcto / rechazado | 302 a la tienda / 200 con «Usuario y/o contraseña incorrectos» |
| Cookies de la sesión | `ASP.NET_SessionId` (la del GET, autenticada tras el POST) y `TS01dc4fc6` (balanceador) |
| Vida de la sesión | 20 min (ASP.NET por defecto) → `VIDA_SESION` |
| Sesión caducada | Redirección a `Acceso`; un servicio puede responder `sin_usuario`/`sin_cliente`/`logout` (según `Scripts/buscar.js`) |
| Antibots | Cloudflare Turnstile **cargado e inactivo**: si se activa, se para |
| ¿Dónde están los productos? | **No en el HTML**: la página los pide con JavaScript a `Productos/buscarJSON.aspx` (JSON) |
| Búsqueda | `buscarJSON.aspx?buscar=<texto>&borrarExistencia=0&ordenarPor=orden inicial&id_categoria=0&id_subcategoria=0&id_marca=&pruebas=false` → `{"Table": [...]}`, todos los resultados en una respuesta |
| ¿Búsqueda exacta por modelo? | No: devuelve «similares» → filtrar por código idéntico |
| Sin resultados | 200 con `Table: []` |
| Precio que paga CCONOR | **Otro servicio**: `buscarJSONprice.aspx?listaproductos=<id>|<id>` → `precio`. El `precio` de la búsqueda es el **de lista** (≈ 1,82 ×) |
| Moneda e IVA | MXN sin IVA. Ninguna respuesta lo dice: lo confirmó el usuario en el portal |
| Id, nombre, marca, código | `id_producto` (interno de Exel), `descripcion`, `Marca`, `codigo_proveedor` (SKU del fabricante, confirmado por el usuario) |
| Existencias | `ExistenciaNacional` (el total; ≥ las demás en los 201 productos) |
| Datos personales en la respuesta | Sí: el agente de ventas de Exel (nombre, correo, celular) y la localidad de CCONOR en **cada** producto |
| `Content-Type` de los servicios JSON | `text/html` (aunque el cuerpo es JSON) |

Lo que no se pueda saber sin peticiones va como **«por confirmar»**: no se inventa.

## 5. Reconocimiento real (Fase 3): protocolo

Las peticiones a un sitio con la cuenta de CCONOR cuentan igual que las llamadas a una API de producción (AGENTS.md §Límites).

### 5.1 Reglas

1. **Presupuesto aprobado antes de la primera petición**, con su propósito. Si una petición deja de tener sentido (p. ej. buscar sin sesión), **no se gasta**: se propone reasignarla y se espera la aprobación.
2. Peticiones **en secuencia, con pausas** (5 s), parando ante el primer 401, 403, 429, 5xx, captcha o página de bloqueo.
3. **Un solo intento de login por aprobación.** Si se rechaza, se para: varios rechazos seguidos pueden bloquear la cuenta. Antes de reintentar, el usuario comprueba sus datos en el navegador.
4. **Una petición por ejecución de script**, o una tanda pequeña con parada ante el primer error. Así nada se repite por accidente.
5. Las respuestas crudas se guardan **solo en el scratchpad de la sesión**, nunca en el repo. Se analizan sin red.
6. **Nunca se imprimen** credenciales, cookies, valores de campos ocultos ni precios reales. De los precios se publican solo tipos, signos, recuentos y **relaciones** (p. ej. «`precio` = 0,55 × `precio_lista` en los 8»).
7. Cada petición se anota en el registro del SDD (fecha, fase, petición, resultado) y en `MEMORY.md`.

### 5.2 Herramientas de reconocimiento (en el scratchpad, nunca en el repo)

| Script | Qué hace |
|---|---|
| `pedir.py <url> <salida>` | Un GET sin seguir redirecciones. Guarda el cuerpo e imprime estado, `Content-Type`, `Location`, tamaño y **solo los nombres** de las cookies |
| `analizar.py <html>` | Sin red: formularios y sus campos (**sin valores**), scripts y rutas de la página |
| `login.py` | GET del formulario + un POST de login con las credenciales leídas de `.env`, sin imprimirlas |
| Análisis de JSON | Claves, tipos y recuentos de cada campo; relaciones entre precios; **nunca valores** |

Ejemplo del núcleo de `pedir.py` (Exel):

```python
r = httpx.get(url, headers=CABECERAS, follow_redirects=False, timeout=20)
salida.write_bytes(r.content)
print("Estado:", r.status_code, r.headers.get("content-type"), "location:", r.headers.get("location"))
print("set-cookie (solo nombres):", [c.split("=", 1)[0] for c in r.headers.get_list("set-cookie")])
```

### 5.3 Cómo encontrar los datos

1. **Página de la tienda con sesión** → ¿trae los productos en el HTML? En Exel, no: solo los de un menú de promociones con el precio vacío y marcadores de carga (`shimmer-item`). Es la señal de que los carga el JavaScript.
2. **Scripts de la página**: listar los `<script src>` y descargar (con autorización) el que busca. En Exel, `Scripts/buscar.js`.
3. **Buscar las llamadas en el script**: `$.getJSON`, `$.ajax`, `fetch(`, `url:` y variables como `sURL = DirectorioRaiz + "…"`. Ahí están las rutas y los parámetros por defecto.
4. **Comparar los precios entre servicios.** En Exel, la búsqueda trae un `precio` que resultó ser el de lista; el de CCONOR estaba en otro servicio. **Nunca dar por bueno el primer campo «precio» que aparezca.**
5. **Preguntar al usuario** lo que el sitio no dice: moneda, IVA y qué es cada código. Se coteja con el portal, como con SYSCOM (docs/30 B4).

## 6. El contrato del scraper

```python
@register
class <Code>Scraper(WebScraperAdapter):
    code = "<code>"                               # igual a Provider.code
    required_credentials = ("USUARIO", "CONTRASENA")  # → PROVIDER_<CODE>_USUARIO / _CONTRASENA
    uses_token = True                             # la sesión (cookies) se guarda como token

    async def request_token(self, client) -> NewToken      # login → NewToken(cookies, VIDA_SESION)
    async def fetch(self, query, client) -> list[Mapping]  # búsqueda (+ precios) → productos crudos permitidos
    def parse_offer(self, item) -> ProviderOffer           # un producto → una oferta
```

Lo que aporta `apps/providers_web/base.py`:

| Pieza | Para qué |
|---|---|
| `WebScraperAdapter._base_url` | `Provider.base_url` sin barra final |
| `WebScraperAdapter._cabeceras(cookie=None, *, ajax=False)` | Cabeceras de navegador (`CABECERAS_NAVEGADOR`), las de AJAX si `ajax=True` y la cookie **explícita**. El `AsyncClient` es compartido: nunca se confía en su almacén de cookies |
| `unir_cookies(*respuestas)` | Las `Set-Cookie` de varias respuestas como cabecera `Cookie` (gana la última; la vaciada se quita) |
| `es_redireccion(respuesta)` | 3xx |

Y `apps/providers_web/extraccion.py`:

| Pieza | Para qué |
|---|---|
| `campos_de_formulario(html, form_id)` | Lo que un navegador enviaría con el formulario sin pulsar botones (ocultos, textos y la opción elegida de cada select). `None` si no existe. Sobre `html.parser`: sin dependencias nuevas |

| Método | Debe | No debe |
|---|---|---|
| `request_token` | GET del formulario, `campos_de_formulario`, POST con los campos + credenciales + botón y la cookie del GET; traducir **el rechazo a `ProviderAuthError` sin `status`**, también un 400/401 del acceso; `raise_for_status` para 403/429/5xx; devolver `NewToken(unir_cookies(get, post), VIDA_SESION)`; formulario o cookies ausentes → `ProviderResponseError("La estructura del sitio cambió …")` | Reintentar el login, guardar la sesión (lo hace `tokens.py`), registrar cookies o credenciales, seguir redirecciones a mano |
| `fetch` | `await self.token(client)`; parámetros **fijos y deterministas** (los de la propia página); detectar la sesión caducada **antes** de `raise_for_status`; validar la forma; recortar a `query.limit` **antes** de pedir precios; quitar los campos no permitidos | Rastrear fichas de detalle, paginar sin decidirlo en el SDD, dormir, reintentar, crear su propio cliente, poner timeouts |
| `parse_offer` | Como en docs/20 §4: precio con `parse_price` (el de CCONOR), 0 se descarta, moneda constante, `raw=item` | `float`, otro `provider_code`, registrar el contenido del producto |

**`VIDA_SESION` debe ser mayor que el margen de renovación del núcleo (5 min)**; si no, se haría un login por búsqueda. Exel usa 20 min.

## 7. Plantilla de código

Basada en [`scrapers/exel.py`](../apps/providers_web/scrapers/exel.py). Los `<…>` salen de la ficha (§4).

```python
"""Scraper de la tienda de <NOMBRE> (SDD <MX>, docs/NN-sdd-mX-<code>-web.md)."""
from __future__ import annotations

from typing import Any, Mapping

import httpx

from apps.providers.adapters.base import NewToken, parse_json, parse_price, raise_for_status
from apps.providers.contracts import ProductQuery, ProviderOffer
from apps.providers.errors import ProviderAuthError, ProviderResponseError
from apps.providers.registry import register
from apps.providers_web.base import WebScraperAdapter, es_redireccion, unir_cookies
from apps.providers_web.extraccion import campos_de_formulario

MONEDA = "<MXN>"            # confirmada con el usuario en el portal
VIDA_SESION = 20 * 60       # > 5 min (margen de renovación del núcleo)
FORMULARIO = "<Form1>"
CAMPO_USUARIO, CAMPO_CONTRASENA = "<name del usuario>", "<name de la contraseña>"
BOTON_ENTRAR = ("<name del botón>", "<value del botón>")
PARAMETROS_FIJOS = {...}    # los que pone la propia página
SIN_SESION = (...)          # lo que responde el sitio sin sesión
CAMPOS_PRODUCTO = (...)     # lista blanca del crudo: nunca datos personales (W9)


@register
class <Code>Scraper(WebScraperAdapter):
    code = "<code>"
    required_credentials = ("USUARIO", "CONTRASENA")
    uses_token = True

    async def request_token(self, client: httpx.AsyncClient) -> NewToken:
        url = f"{self._base_url}/<ruta del login>"
        formulario = await client.get(url, headers=self._cabeceras())
        self._rechazo_del_acceso(formulario)
        raise_for_status(self.code, formulario)
        campos = campos_de_formulario(formulario.text, FORMULARIO)
        if not campos or CAMPO_USUARIO not in campos or CAMPO_CONTRASENA not in campos:
            raise ProviderResponseError(self.code, "La estructura del sitio cambió (formulario de acceso).")
        datos = {**campos, CAMPO_USUARIO: self.credentials["USUARIO"],
                 CAMPO_CONTRASENA: self.credentials["CONTRASENA"], BOTON_ENTRAR[0]: BOTON_ENTRAR[1]}
        respuesta = await client.post(url, data=datos,
                                      headers={**self._cabeceras(unir_cookies(formulario)), "Referer": url})
        if <la respuesta es el rechazo documentado en la ficha>:
            raise ProviderAuthError(self.code, "<NOMBRE> rechazó el inicio de sesión.")  # sin status
        self._rechazo_del_acceso(respuesta)
        raise_for_status(self.code, respuesta)
        sesion = unir_cookies(formulario, respuesta)
        if not es_redireccion(respuesta) or not sesion:
            raise ProviderResponseError(self.code, "Respuesta de inicio de sesión inesperada.")
        return NewToken(sesion, VIDA_SESION)

    async def fetch(self, query: ProductQuery, client: httpx.AsyncClient) -> list[Mapping[str, Any]]:
        cookie = await self.token(client)
        respuesta = await client.get(f"{self._base_url}/<ruta de búsqueda>",
                                     params={"<texto>": _recortar(query.sku or query.term), **PARAMETROS_FIJOS},
                                     headers=self._cabeceras(cookie, ajax=True))
        datos = self._json(respuesta)
        productos = <la lista dentro de datos>
        if not isinstance(productos, list):
            raise ProviderResponseError(self.code, "La estructura del sitio cambió (búsqueda).")
        if query.sku:
            productos = [p for p in productos if <código idéntico a query.sku>]
        productos = productos[: query.limit]
        return [<solo CAMPOS_PRODUCTO> + <su precio de CCONOR, si va aparte> for p in productos]

    def _json(self, respuesta: httpx.Response) -> Any:
        if <sesión caducada: redirección, SIN_SESION o la página de login>:
            raise ProviderAuthError(self.code, "La sesión de <NOMBRE> caducó.", status=401)  # el núcleo renueva
        raise_for_status(self.code, respuesta)
        return parse_json(self.code, respuesta)  # aunque el Content-Type diga text/html

    def _rechazo_del_acceso(self, respuesta: httpx.Response) -> None:
        if respuesta.status_code in (400, 401):
            raise ProviderAuthError(self.code, f"<NOMBRE> rechazó el inicio de sesión (HTTP {respuesta.status_code}).")
```

`parse_offer` y las funciones `_texto`, `_existencias`, `_identificador` siguen el patrón de docs/20 §5 y de `exel.py`. **Se copian al módulo nuevo, no se importan de otro scraper**: cada uno es independiente. Si un tercer scraper las necesita idénticas, se propone moverlas a `base.py`.

### 7.1 Registro

```python
# apps/providers_web/scrapers/__init__.py
from . import exel  # noqa: F401  (M9, docs/31-sdd-m9-exel-web.md)
from . import <modulo>  # noqa: F401  (<MX>, docs/NN-sdd-mX-<code>-web.md)
```

Archivo con nombre `^[a-z0-9_]+$` (G5); un `code` con guiones va con guion bajo en el nombre del archivo, como en docs/20 §5.4.

### 7.2 Variantes

| Si el sitio… | Entonces |
|---|---|
| Muestra precios **sin sesión** | `uses_token = False`, sin `request_token` ni credenciales |
| Trae los productos **en el HTML** (no hay JSON) | Extraer con `html.parser`. Ampliar `extraccion.py` con un extractor **genérico** (por etiqueta, clase o atributo) y sus pruebas; lo específico del sitio va en el scraper |
| Trae el precio en la misma respuesta que los productos | Una sola petición por búsqueda. **Comprobar antes** que es el precio de CCONOR y no el de lista (§5.3) |
| Pagina los resultados | Una sola página salvo decisión del SDD (cortesía, W4) |

## 8. Reglas de traducción

Las de docs/20 §6 valen tal cual (`external_id` en texto, nombre no vacío, precio `Decimal` y 0 descartado, moneda constante, `mpn` solo si es del fabricante, `stock` entero ≥ 0). Además, en un sitio web:

| Regla | Por qué (Exel) |
|---|---|
| **Precio de CCONOR, no el primero que aparezca** | El `precio` de la búsqueda era el de lista: Exel habría salido casi al doble en el comparador |
| **Lista blanca para `raw`** (`CAMPOS_PRODUCTO`) | Cada producto traía el nombre, el correo y el celular del agente de ventas. Con lista blanca, un campo personal nuevo nunca llega a `raw` |
| **`sku` sin búsqueda exacta → filtrar por código idéntico** | El sitio devuelve «similares»: el comparador empareja por clave exacta |
| **Recortar a `limit` antes de pedir precios** | No pedir precios de productos que se van a descartar |
| **Recortar el término al `maxlength` del buscador** | Exel admite 100 caracteres; la consulta nunca falla por formato |
| **Leer JSON con `parse_json` aunque el `Content-Type` sea `text/html`** | Los servicios de Exel anuncian HTML y devuelven JSON |
| **`image_url` y `product_url` solo con ids seguros** (`[A-Za-z0-9._-]`) | Se pintan en HTML; el contrato exige `https` |
| **La ficha del producto puede exigir sesión** | En Exel abre el login si el asesor no ha entrado. Se documenta |

## 9. Errores: qué hace cada quien

| Situación | Quién | Resultado |
|---|---|---|
| Faltan credenciales | Núcleo | `ProviderConfigError`, sin peticiones |
| Login rechazado (página con el error, redirección de vuelta al login, 400/401) | **Scraper** (`ProviderAuthError` **sin `status`**) | Error, **un solo** intento |
| Sesión caducada en una búsqueda | **Scraper** (`status=401`) + núcleo | El núcleo renueva y reintenta una vez; si sigue caducada, error 401 |
| 403 / 429 / 5xx | `raise_for_status` | `ProviderAuthError(403)` / `ProviderRateLimitError` / `ProviderResponseError`, sin reintento |
| Formulario, cookies o respuesta con otra forma | **Scraper** | `ProviderResponseError("La estructura del sitio cambió (…).")`: **nunca** una lista vacía silenciosa |
| «Sin resultados» documentado | **Scraper** (`[]`) | `ok`, 0 ofertas |
| Producto inválido o sin precio de CCONOR | Scraper lanza; núcleo descarta | `discarded` + 1 |
| Captcha o desafío antibots | — | Se ve como login rechazado o estructura cambiada. **No se evade**: se habla con el proveedor |

Los mensajes nunca llevan cookies, credenciales ni el contenido de las páginas.

## 10. Pruebas (Fase 4)

Runner de Django, **sin red y sin credenciales reales**. Se reutiliza `apps/providers/tests/soporte.py` (`ApiFalsa`, `sin_red()`, `credenciales_de_prueba()`, `exigir_bd_de_pruebas()`), importando solo funciones y constantes.

### 10.1 Archivos

| Archivo | Contenido |
|---|---|
| `apps/providers_web/tests/test_scraper_<modulo>.py` | Pruebas del scraper con identificadores propios (Exel: E1–E28) |
| `apps/providers_web/tests/test_extraccion.py` | Si se amplía `extraccion.py` (Exel: X1–X6) |
| `apps/providers_web/tests/muestras/<modulo>_*.json` / `.html` | Estructura real, **valores ficticios** |

### 10.2 Muestras saneadas

- **JSON**: se generan con un script **en el scratchpad** que copia de las respuestas reales **solo las claves y los tipos**, pone valores neutros y después fija a mano los campos que usan las pruebas (ids `EXM…900001`, «DE MUESTRA», `@ejemplo.invalid`). Los campos personales se rellenan con valores ficticios, para probar que **no** llegan a `raw`.
- **Comprobación de fugas**: otro script busca en las muestras cada texto (≥ 5 caracteres) y cada número de las respuestas reales. Solo pueden coincidir nombres de campos y palabras genéricas.
- **HTML del login**: recortado a mano con los **nombres reales** de los campos y valores ficticios, más un formulario ajeno para probar que no se mezcla.
- Se sirven como bytes con el `Content-Type` real (`httpx.Response(200, content=…, headers={"content-type": "text/html; charset=utf-8"})`), y se leen en las pruebas con `parse_float=Decimal`.

### 10.3 Catálogo mínimo

| Bloque | Casos |
|---|---|
| Registro | `code` en `registered_codes()`, subclase de `WebScraperAdapter`, `required_credentials`, `uses_token` |
| `request_token` | Formulario **exacto** enviado (ocultos, select, credenciales, botón; sin campos de otro formulario ni otros botones) · la cookie del POST va **explícita** (prueba con un cliente que vacía su almacén de cookies) · cookies nuevas del POST · rechazo → sin `status` y sin secretos en el mensaje · redirección de vuelta al login · 400/401 en GET y POST sin `status` · 403/429/5xx · formulario o cookies ausentes · `base_url` con y sin barra |
| `fetch` | Parámetros exactos y cabeceras (cookie, AJAX) · término normalizado y recortado · `sku` manda y filtra por código idéntico · `limit` antes de los precios · sin resultados → `[]` sin pedir precios · otra forma → «La estructura del sitio cambió» · **cada** señal de sesión caducada → 401 (incluida la página de login **sin** la palabra «logout») · 404/5xx/JSON inválido/429 · `raw` sin campos personales y con su precio · ids inválidos |
| `parse_offer` | La muestra da exactamente la oferta esperada · cada producto inválido lanza una excepción descartable · marca, código, existencias y foto inválidos → `None` sin descartar · ids con caracteres raros → sin URL |
| `search_all` | Búsqueda completa con descartes y la sesión guardada en `ProviderToken` · **una** sesión para dos búsquedas · sesión caducada renovada una vez · sin un tercer intento · login rechazado = un solo POST · sin credenciales = 0 peticiones · centinelas de contraseña y cookie ausentes de logs y errores · sin resultados = `ok` |
| Reglas | G2, G4 y G10 ya cubren `apps/providers_web` |

### 10.4 Cobertura y mutaciones

```powershell
coverage run --branch --data-file=$env:TEMP\cconor.coverage --source=apps/providers_web --omit="apps/providers_web/tests/*" manage.py test apps.providers_web
coverage report --data-file=$env:TEMP\cconor.coverage -m
```

Objetivo: **100 % de líneas y ramas**. Las mutaciones se hacen con un script del scratchpad que rompe cada regla de una en una (moneda, campo del precio, recortes, filtro del `sku`, detección de sesión, rechazo sin `status`, lista blanca, cookie explícita…), corre `manage.py test apps.providers_web --keepdb --failfast` y **restaura los bytes desde memoria**. Una mutación que sobrevive es una prueba que falta o **código redundante**: en Exel, dos supervivientes eran comprobaciones que el contrato ya hacía y se quitaron.

## 11. Decisiones estándar para el SDD

Además de P1–P16 (docs/20 §10), cada scraper responde estas, con alternativas y una recomendada. La recomendada por defecto es la que se tomó con Exel ([31 §6.2](31-sdd-m9-exel-web.md)).

| # | Pregunta | Recomendada por defecto | Por qué |
|---|---|---|---|
| W1 | ¿De dónde salen los datos? | El servicio JSON que usa la propia página; si no hay, el HTML con `extraccion.py` | Más estable que el HTML y con `Decimal` directo |
| W2 | ¿Y si solo hay JavaScript? | Parar y replantear | Navegadores sin interfaz prohibidos |
| W3 | ¿Cómo se mantiene la sesión? | `uses_token=True`; `request_token` hace el login y devuelve las cookies | Una sesión para muchas búsquedas, sin tocar M2 |
| W4 | Cortesía | Una página por búsqueda, sin fichas de detalle, sin reintentos ni esperas ante un 429, `robots.txt` respetado, sin evadir antibots | Cuenta de CCONOR y límites desconocidos |
| W5 | ¿Y si el sitio cambia? | Error explícito «La estructura del sitio cambió» | Un `[]` silencioso haría creer que no vende el producto |
| W6 | Redirecciones | No seguirlas (`follow_redirects=False`, como el cliente del núcleo) | Así se detecta la redirección al login |
| W7 | `timeout_ms` | 10000 | Login + búsqueda + precios en HTML/JSON pesado |
| W8 | ¿Qué se guarda como token? | Todas las cookies de la sesión, como cabecera `Cookie` | El balanceador puede exigir las suyas |
| W9 | ¿Qué va en `raw`? | Lista blanca de campos del producto | Sin datos personales de terceros |
| W10 | ¿De dónde sale el precio? | El que paga CCONOR, comparado entre servicios con el reconocimiento | El primero que aparece puede ser el de lista |
| W11 | `sku` sin búsqueda exacta | Buscar el código y quedarse con los de código idéntico | Emparejamiento por clave exacta |
| W12 | Muestras | Generadas desde las respuestas reales copiando solo claves y tipos | Fieles a la estructura y sin datos reales |

## 12. Archivos que crea o toca una integración

| Crea | Toca | No toca |
|---|---|---|
| `apps/providers_web/scrapers/<modulo>.py` | `apps/providers_web/scrapers/__init__.py` (el import) | M2: `contracts.py`, `errors.py`, `adapters/base.py`, `registry.py`, `tokens.py`, `service.py` |
| `apps/providers_web/tests/test_scraper_<modulo>.py` y `muestras/<modulo>_*` | `.env_example` (solo los nombres `PROVIDER_<CODE>_*`) | Otros scrapers y adaptadores |
| `docs/NN-sdd-mX-<code>-web.md` y `docs/NN+1-scraper-<code>-web.md` | `settings/settings.py`: `CCONOR_PROVIDER_PRICE_BASES['<code>']` **solo** con la base de IVA confirmada | `requirements*.txt` (nada de dependencias nuevas) |
| | `apps/quotes/views.py`: `CONFIANZA_POR_PROVEEDOR['<code>']` **solo** si `model`/`mpn` son del fabricante (con su prueba, como `test_v10c`) | Ninguna migración |
| | `static/js/api.js` (`PROVIDER_LABELS`) y la clase `badge-<code>` (CSS, `base.html`, PDF) si el proveedor se muestra con insignia propia | |
| | `docs/30` (su fila de pendientes), `MEMORY.md`, `AGENTS.md` (§Arquitectura: el scraper nuevo) | |

`apps/providers_web/base.py` y `extraccion.py` solo se tocan para añadir algo **genérico** que necesite el nuevo sitio, con sus pruebas; nunca para algo específico de un proveedor.

## 13. Verificación contra el sitio real (Fase 5)

1. Dar de alta el `Provider` (admin, o `manage.py shell` con autorización): `code`, `name`, `base_url`, `timeout_ms` = 10000 y `active` solo con credenciales en `.env`.
2. Presupuesto aprobado: **dos** `search_all` seguidas, para comprobar que la segunda reutiliza la sesión. Ojo: `search_all` consulta **todos** los proveedores activos (con Exel, 2 GET de SYSCOM).
3. Script en el scratchpad con un **transporte que cuenta** las peticiones por ruta y delega en el real. No imprime precios: solo tipo, signo y moneda.

```python
class Contador(httpx.AsyncBaseTransport):
    def __init__(self):
        self.real, self.rutas = httpx.AsyncHTTPTransport(), Counter()

    async def handle_async_request(self, request):
        self.rutas[f"{request.method} {request.url.host}{request.url.path}"] += 1
        return await self.real.handle_async_request(request)

resultados = await search_all(ProductQuery(term="camara ip", limit=3), transport=contador)
```

Se ejecuta con `venv/Scripts/python.exe manage.py shell -v 0 -c "exec(open(r'<ruta>', encoding='utf-8').read())"`. Esperado con Exel: 1 GET + 1 POST de login, 2 búsquedas, 2 consultas de precios y una sola fila en `ProviderToken`.

## 14. Criterio de aceptación

| Comprobación | Esperado |
|---|---|
| `python manage.py check` y `makemigrations --check --dry-run` | Sin errores ni cambios; el `code` en `registered_codes()` |
| `python manage.py test` | Todo en OK, incluidas G1–G10 (salvo fallos previos ajenos, documentados) |
| Cobertura de `apps/providers_web` | 100 % de líneas y ramas |
| Mutaciones | Todas detectadas (las supervivientes, justificadas o convertidas en simplificaciones) |
| Muestras | Comprobación de fugas sin valores reales |
| Verificación real | Ofertas `Decimal` > 0 en la moneda confirmada, una sola sesión para dos búsquedas, peticiones = presupuesto |
| Documentación | SDD con el registro de peticiones y documento final de funcionamiento y conexiones |

## 15. Errores a evitar (aprendidos con Exel)

- **Credenciales mal escritas = logins rechazados reales.** El usuario las comprueba en el navegador antes del primer intento automático, y tras un rechazo se para.
- **Nunca mostrar HTML de respuesta sin ocultar los `value` de los campos de texto.** Exel devuelve el usuario escrito en el formulario y una comparación lo imprimió una vez.
- **Con `python -I` no se lee `PYTHONIOENCODING`.** Un `print` con «→» rompió un script a mitad, después del login y antes de la búsqueda, y costó otro login. Usar `sys.stdout.reconfigure(encoding="utf-8")`.
- **La primera búsqueda con sesión puede no traer productos**: los carga el JavaScript. Mirar los scripts antes de gastar más peticiones.
- **El primer `precio` puede ser el de lista.** Comparar con el servicio de precios de la cuenta.
- **El `Content-Type` miente**: JSON anunciado como `text/html`.
- **Las respuestas traen datos personales** (agentes, localidades): lista blanca en `raw` y en las muestras.
- **El cliente compartido guarda cookies**: la cookie va siempre explícita, y se prueba con un cliente que olvida las suyas.
- **Una señal de sesión caducada puede tapar a otra en las pruebas.** La página de login rechazado contenía «logout»: probar cada señal por separado.
- **Un `[]` silencioso es el peor fallo de un scraper**: cuando falta la estructura esperada, error explícito.
- **No ejecutar `python` a secas en Git Bash** y no usar `git restore` en mutaciones (docs/20 §13).

## 16. Plantilla de prompt para arrancar una integración

```text
Integra la tienda web de <NOMBRE> (code "<code>", sin API) en el Cotizador CCONOR por web
scraping, siguiendo el estándar de docs/33-guia-integrar-proveedor-por-web-scraping.md.

Lee antes: AGENTS.md (manda sobre todo), MEMORY.md, docs/33, docs/20 y, como ejemplo
completo, docs/31 y docs/32 junto con apps/providers_web/scrapers/exel.py.
Tienda: <URL>. CCONOR confirma el permiso de uso automatizado: <sí/no>. Cuenta de cliente
en .env como PROVIDER_<CODE>_USUARIO / _CONTRASENA: <sí/no>, comprobada en el navegador: <sí/no>.

Antes de escribir código:
1.- Rellena la ficha del sitio (docs/33 §4); lo no confirmado va "por confirmar".
2.- Propón el SDD (docs/NN-sdd-mX-<code>-web.md) con P1–P16 (docs/20 §10) y W1–W12
    (docs/33 §11), cada una con alternativas y una recomendada.
3.- Propón el presupuesto del reconocimiento (docs/33 §5), petición por petición.
No se hace ninguna petición al sitio sin presupuesto aprobado. La última tarea es el
documento final docs/NN+1-scraper-<code>-web.md y actualizar MEMORY.md y docs/30.
```
