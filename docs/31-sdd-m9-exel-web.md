# 31 · SDD de M9: Exel del Norte por web scraping (`apps/providers_web`)

## 1. Identificación

| | |
|---|---|
| **Módulo** | M9: extracción web de proveedores sin API (`apps/providers_web`) y su primer scraper, Exel del Norte (`code="exel"`) |
| **Rama** | `feature/web-scraping-excel-del-norte`, creada desde `main` en `7f5f753` |
| **Estado** | IMPLEMENTADO Y VERIFICADO contra el sitio real (2026-10-27): 41 pruebas (E1–E28, X1–X6), 100 % de líneas y ramas de `apps/providers_web`, 43 de 43 mutaciones detectadas y verificación con `search_all` (§10). Funcionamiento en [32](32-scraper-exel-web.md) |
| **Depende de** | M2: contrato y orquestación ([13](13-sdd-m2-contrato-y-orquestacion.md), [14](14-contrato-y-orquestacion.md)); M1a: `Provider` y `ProviderToken` ([02](02-modelo-de-datos.md)) |
| **Sigue** | El estándar de [20](20-guia-integrar-api-de-proveedor.md), **adaptado a un sitio web**: mismas fases, mismo contrato (`ProviderAdapter`), mismas reglas de pruebas y de llamadas reales. Toma como ejemplo completo a SYSCOM ([16](16-sdd-m3-syscom.md), [17](17-adaptador-syscom.md)) |
| **Refina** | docs/30 B5, que esperaba «acceso real a la API» de Exel: Exel no da API y los datos se obtienen de su tienda web |

## 2. Propósito y alcance

Exel del Norte no ofrece API. M9 permite que `search_all` consulte su tienda web (`https://www.exel.com.mx/xlstore/`) con la cuenta de cliente de CCONOR y devuelva sus productos como `ProviderOffer`, igual que SYSCOM.

**Por qué una app aparte (decisión del usuario, 2026-10-27).** La extracción web es otra forma de obtener datos para el cotizador, pensada para cualquier proveedor sin API. Se separa de las APIs (`apps/providers`) para que las dos partes no se mezclen:

| `apps/providers` (APIs) | `apps/providers_web` (web scraping) |
|---|---|
| Núcleo M2 (contratos, registro, tokens, `search_all`) y modelos | Herramientas de extracción web y los scrapers por proveedor |
| `adapters/<code>.py` | `scrapers/<code>.py` |
| No importa de `providers_web` (regla G10) | Importa de `providers` y nada más (regla G4) |

Esto **refina** la regla de AGENTS.md «Todos [los proveedores] viven dentro de apps/providers/»: los proveedores **con API** siguen ahí; los proveedores **sin API** son scrapers en `apps/providers_web/scrapers/<code>.py`. Se propone actualizar AGENTS.md al cierre (Fase 6).

**Entra:** búsqueda por texto y por modelo; inicio de sesión con la cuenta de CCONOR (si los precios lo exigen); pruebas sin red; documentación final en `docs/32`.

**No entra:** descargar o rastrear el catálogo completo; abrir fichas de detalle por cada producto; guardar el crudo en `RawProviderProduct`; navegadores sin interfaz (Playwright, Selenium); nuevas dependencias; el enlace con `Supplier`; la base de IVA en `CCONOR_PROVIDER_PRICE_BASES` hasta confirmarla (B5); alinear el `'ct'` del mock de `static/js/api.js`.

## 3. Arquitectura y dependencias

```
search_all (M2, sin cambios) ─► registry ─► apps/providers/adapters/syscom.py    (API)
                                       └──► apps/providers_web/scrapers/exel.py   (web)
```

- `ProvidersWebConfig.ready()` importa `apps.providers_web.scrapers`; cada scraper se registra con `@register` de `apps.providers.registry`. Así `search_all` lo consulta como a cualquier `Provider` activo, con su `timeout_ms`, su resultado propio y el descarte de ofertas inválidas.
- **M2 sigue congelado**: no se tocan `contracts.py`, `errors.py`, `adapters/base.py`, `registry.py`, `tokens.py` ni `service.py`.
- Sin modelos ni migraciones: el `Provider "exel"` está en la tabla de `providers` y se da de alta en el admin (P8). La cookie de sesión, si hace falta, se guarda en `ProviderToken` como un token (W3).
- Sin dependencias nuevas: `httpx` para las peticiones y `html.parser` de la biblioteca estándar para el HTML.

## 4. Ficha del sitio (Fase 1; se confirma en la Fase 3)

Lo que no se puede saber sin peticiones va como **por confirmar**: no se inventa.

| Pregunta | Exel del Norte |
|---|---|
| URL base (va en `Provider.base_url`) | `https://www.exel.com.mx/xlstore/` (dada por el usuario) |
| Plataforma de la tienda | **Confirmado (2026-10-27):** ASP.NET WebForms, con `__VIEWSTATE`, `__EVENTVALIDATION` y `UpdatePanel`. Servidor `volt-adc`. Cookies `ASP.NET_SessionId` y `TS01dc4fc6`; esta última es de un balanceador/firewall de aplicaciones |
| Codificación | **Confirmado:** UTF-8 (cabecera y cuerpo) |
| Formato de la búsqueda | Por confirmar (W1). El buscador `txtBusqueda` (máx. 100 caracteres) llama a `txtBusqueda_keypress`, definida en `Scripts/Master.js` (no descargado). La página carga `instantsearch.production.min.js`: el buscador podría ser Algolia (JSON). Las categorías se listan en `/xlstore/Productos/buscar.aspx?IdCategoria=<n>` |
| ¿Necesita JavaScript para mostrar productos? | Por confirmar (W2) |
| `robots.txt` | **Confirmado:** `User-agent: *` / `Disallow: /Contenido/*`. `/xlstore/` está permitido |
| Autenticación | **Toda la tienda exige sesión:** `GET /xlstore/` → 302 a `/xlstore/Acceso` sin sesión. Login = *postback* de WebForms `POST /xlstore/Acceso`: los ocultos `__VIEWSTATE`, `__VIEWSTATEGENERATOR` y `__EVENTVALIDATION` del GET previo, más `ctl00$MainContent$txtUsuario`, `ctl00$MainContent$txtPassword` y el botón `ctl00$MainContent$L_ButtonLogin`. La validación del cliente rechaza usuario o contraseña con espacios. La geolocalización está comentada (no se exige). Respuesta del login: por confirmar |
| Cloudflare Turnstile | **Cargado pero inactivo hoy:** el botón lleva la clase `btn-cloudflare-turnstile` y existe el oculto `ctl00$hdnTokenCloudflare`, pero `inicializarTurnstile()` solo habilita los botones, sin pedir un desafío. **Riesgo:** si Exel lo activa, el login automático deja de ser posible sin evadir un captcha, y entonces se para (W4, W8) |
| ¿Los precios de CCONOR solo se ven con sesión? | **Sí:** sin sesión no se ve ninguna página de la tienda |
| Vida de la sesión | Por confirmar. Una sesión ASP.NET suele durar 20 minutos sin actividad; propuesta: `VIDA_SESION` = 15 min (W3) |
| Cómo se detecta una sesión caducada | Esperado: 302 a `/xlstore/Acceso`, igual que sin sesión (W3, W6) |
| URL y parámetros de la búsqueda por texto | **Confirmado (página):** `GET /xlstore/Productos/buscar.aspx?busqueda=<texto>` (también `?IdCategoria=<n>[&IdSubCategoria=<m>]`). **Pero la página no trae los productos:** los pide `Scripts/buscar.js` a un servicio aún sin identificar (W1, por confirmar) |
| Tarjeta de producto (menú) | `div.vista-previa-producto` → `a.lnk-descripcion` (nombre), `a.lbl-codigo` («Código: <código>»), enlace `Productos/Detalle/<clave>`, foto `https://contenidos.exel.com.mx/imgProducto/<clave>.png` y `div.costo-detalle`, vacío en el menú. Puede servir de pista para los resultados, pero no se da por igual |
| **Respuesta de `buscarJSON.aspx`** (confirmada el 2026-10-27, 201 productos de «camara ip») | Objeto con `Table` (productos) y `Table1`–`Table9` (facetas que no se usan). Tipos estables en los 201: `id_producto` str (código interno de Exel, con forma `AAAAAAAA999`), `Articulo` y `descripcion` str (nombre), `Marca` str (p. ej. DAHUA, TP-LINK), `codigo_proveedor` str, nunca vacío y con forma de número de parte del fabricante (`DH-IPC-HDW…`, `IPC-T…`, `CAM-…`), `precio` número decimal (4 decimales, >0 en los 201), `Existencia`/`ExistenciaMX`/`ExistenciaNacional` int (`Existencia`=0 en 189 de 201) y `TieneExistencia*` int 0/1, `id_unidad` str (`PZA`), `tieneFoto` int. Se descodifica con `parse_json` (texto, `parse_float=Decimal`) aunque el `Content-Type` diga `text/html` |
| **Datos personales en el crudo** | Cada producto trae los datos del agente de ventas de Exel (`nombre_agente`, `correo_agente`, `celular_agente`, `horario_agente`, `departamento_agente`) y la localidad de CCONOR (`descripcion_localidad_cliente`, `id_localidad_cliente`). **No van en `raw` ni en las muestras** (W9) |
| **Precio que paga CCONOR** | **Confirmado (2026-10-27, 8 productos):** el de `Productos/buscarJSONprice.aspx?listaproductos=<id>|<id>…`, campo `precio` (2 decimales). Esa respuesta es una lista con un objeto por id: `id_producto`, `codigo_proveedor` (igual al de la búsqueda), `precio`, `precio_lista`, `precio_normal`, `origen` (`N` normal, `P` promoción), `vigencia`, escalas, regalos, planes y carepacks. Relaciones: `precio_lista` = el `precio` de `buscarJSON` en los 8 (**el `precio` de la búsqueda es el de lista, no el de CCONOR**), y `precio` = 0.55 × `precio_lista` en los 8. `precio_normal` = `precio_lista` en 3 y = `precio` en 5. Sin descuentos por volumen ni promociones en la muestra. Si el rol del usuario no ve precios («SOLO BUSCADOR Y EXISTENCIAS», «CONSULTA SIN PRECIOS, SIN CARRITO»), la página los oculta |
| Búsqueda por modelo | **Confirmado:** no hay búsqueda exacta. `buscarJSON.aspx?buscar=<codigo_proveedor>` devolvió 1 resultado con `codigo_proveedor` idéntico (`opcion` = `similares`) |
| «Sin resultados» | **Confirmado:** 200 con `Table: []` (y el resto de tablas vacías salvo `Table8`, segmentación) |
| Moneda e IVA | **Confirmado por el usuario en el portal (2026-10-27): MXN sin IVA.** Ninguna respuesta lo indica (el formato lo da `DarFormatoMoneda`, en otro script) |
| Código del producto | **Confirmado por el usuario (2026-10-27):** `codigo_proveedor` (el «Código» de la tienda) es el SKU del fabricante → `model` y `mpn`. El código interno de Exel es `id_producto` → `external_id` |
| Existencias | `ExistenciaNacional` es el total: en los 201 productos es ≥ `Existencia` y ≥ `ExistenciaMX`, y `TieneExistencia` = (`ExistenciaNacional` > 0) en todos → `stock` |
| Nombre | `descripcion` es el texto que pinta la página (más largo que `Articulo` en 73 de 201) → `name` |
| Página de «sin resultados» | `div#no-resultados` (oculto por defecto) con «No se encontraron resultados con las condiciones indicadas…»; lo muestra el JavaScript, así que el marcador real depende del servicio de búsqueda |
| Búsqueda exacta por modelo / número de parte | Por confirmar |
| Paginación y tamaño de página | Por confirmar (P6) |
| Página de «sin resultados» | Por confirmar: el marcador exacto (W5) |
| Identificador del producto | Por confirmar |
| Nombre, marca, modelo | Por confirmar |
| ¿El modelo es el número de parte del fabricante? | Por confirmar (P11); docs/21 ya advierte no pasar el SKU de Exel como MPN sin validarlo |
| Precio que paga CCONOR, formato | Por confirmar. Se espera texto con formato (`$1,234.50`) que se convierte a `Decimal` sin `float` |
| Moneda e IVA | Por confirmar (P2, B5) |
| Existencias | Por confirmar (P7) |
| Foto y ficha | Por confirmar; solo se aceptan URLs `https` del dominio `exel.com.mx` |
| Protección antibots (captcha, Cloudflare, límites) | Por confirmar. Si aparece, se para (W4) |

## 5. Contrato

```python
@register
class ExelScraper(WebScraperAdapter):        # WebScraperAdapter hereda de ProviderAdapter
    code = "exel"
    required_credentials = ("USUARIO", "CONTRASENA")  # → PROVIDER_EXEL_USUARIO / _CONTRASENA en .env
    uses_token = True                                 # la cookie de sesión se guarda como token (W3)

    async def request_token(self, client) -> NewToken      # inicia sesión → NewToken(cookie, VIDA_SESION)
    async def fetch(self, query, client) -> list[Mapping]  # una página de resultados → productos crudos
    def parse_offer(self, item) -> ProviderOffer           # un producto → una oferta
```

El reconocimiento confirmó el login: `uses_token = True` y `required_credentials = ("USUARIO", "CONTRASENA")` se quedan.

### 5.1 Archivos

| Archivo | Estado | Contenido |
|---|---|---|
| `apps/providers_web/apps.py` | Hecho | `ProvidersWebConfig`, `ready()` importa `scrapers` |
| `apps/providers_web/scrapers/__init__.py` | Hecho | `from . import exel` |
| `apps/providers_web/base.py` | Hecho | `WebScraperAdapter` (URL de la tienda, cabeceras de navegador y AJAX, cabecera `Cookie` explícita por petición), `unir_cookies` y `es_redireccion` |
| `apps/providers_web/extraccion.py` | Hecho | `campos_de_formulario(html, form_id)` sobre `html.parser`: lo que un navegador enviaría con el formulario. Se usa para el login (W1 resultó JSON para la búsqueda) |
| `apps/providers_web/scrapers/exel.py` | Hecho | `ExelScraper` |
| `apps/providers_web/tests/` | Hecho | `test_scraper_exel.py` (E1–E28), `test_extraccion.py` (X1–X6), `muestras/exel_*.json` y `exel_*.html` |
| `settings/settings.py` (`CCONOR_PROVIDER_PRICE_BASES`) | Hecho | `'exel': {'vat_basis': 'NET'}` |
| `settings/settings.py` | Hecho | `'apps.providers_web'` en `INSTALLED_APPS` |
| `apps/providers/tests/test_reglas_repo.py` | Hecho | G2 y G4 cubren `apps/providers_web`; G10 nueva (detectada con una mutación) |
| `.env_example` | Hecho | `PROVIDER_EXEL_USUARIO`, `PROVIDER_EXEL_CONTRASENA` (solo los nombres) |

## 6. Decisiones

### 6.1 Estándar de docs/20 §10 (P1–P16)

Se toma la recomendada por defecto salvo donde se indica.

| # | Decisión | Para Exel |
|---|---|---|
| P1 | Modo de consulta | Solo búsqueda bajo demanda con `search_all` |
| P2 | Moneda e impuestos | `currency="MXN"`, sin IVA (confirmado en el portal). `CCONOR_PROVIDER_PRICE_BASES['exel'] = {'vat_basis': 'NET'}` (B5) |
| P3 | Guardar el crudo | No en M9 (se revisa aparte, aunque el scraping esté permitido) |
| P4 | Búsqueda por `sku` | Búsqueda exacta si existe; manda sobre `term`. Si no existe, `sku` se envía como texto de búsqueda y se filtra por modelo igual |
| P5 | Límites de formato | Ajustar y recortar; la consulta nunca falla por formato |
| P6 | Paginación | **Una sola página** (cortesía, W4), aunque su tamaño sea menor que `limit` |
| P7 | Existencias | Total en `stock`, sin filtrar por stock |
| P8 | Alta del `Provider` | Manual en el admin, `timeout_ms` = 10000 (W7) |
| P9 | Muestras | Estructura real, valores ficticios, sin cookies |
| P10 | Qué precio | El que paga CCONOR con su sesión, nunca el público; el 0 se descarta |
| P11 | `model` y `mpn` | `codigo_proveedor` en los dos: es el SKU del fabricante (confirmado), como el `modelo` de SYSCOM. Así Exel y SYSCOM se emparejan por clave exacta |
| P12 | Credenciales rechazadas al iniciar sesión | `ProviderAuthError` sin `status`: no se repite el login |
| P13 | «Sin resultados» | Solo el marcador exacto de la página; lo demás es error |
| P14 | Variables de `.env` | `PROVIDER_EXEL_<CLAVE>`; solo los nombres en `.env_example` |
| P15 | Llamadas reales | Las hace el agente con presupuesto cerrado y registro (§9, §10) |
| P16 | Enlace con `Supplier` | Fuera de M9 |

### 6.2 Propias de un sitio web (W1–W7)

| # | Pregunta | Alternativas | Recomendada |
|---|---|---|---|
| W1 | ¿De dónde salen los datos? | A) El endpoint JSON que usa el propio buscador de la tienda, si existe. B) El HTML de la página de resultados | **A si existe** (estructura estable, `parse_json` con `Decimal`); si no, B con `extraccion.py` |
| W2 | ¿Y si los productos solo aparecen con JavaScript? | A) Parar y replantear. B) Navegador sin interfaz | **A**: B añade dependencias pesadas, prohibidas por AGENTS.md |
| W3 | ¿Cómo se mantiene la sesión? | A) `uses_token=True`: `request_token` hace el login y devuelve la cookie como `NewToken(cookie, VIDA_SESION)`, que el núcleo guarda en `ProviderToken`. B) Iniciar sesión en cada búsqueda | **A**: una sesión para muchas búsquedas, reutiliza M2 sin tocarlo, y la sesión caducada se renueva una sola vez (401 → reintento del núcleo) |
| W4 | Cortesía con el sitio | A) Una página por búsqueda, sin fichas de detalle, sin reintentos ni esperas ante un 429, respetando `robots.txt`, sin evadir captchas ni bloqueos. B) Rastrear fichas para completar datos | **A** |
| W5 | ¿Y si el sitio cambia su HTML? | A) Error explícito `ProviderResponseError("La estructura del sitio cambió.")`. B) Devolver `[]` | **A**: un `[]` silencioso haría creer que Exel no vende el producto |
| W6 | Redirecciones | A) `follow_redirects=False` (como ya crea el cliente el núcleo) y detectar la redirección a login. B) Seguirlas | **A** |
| W7 | `timeout_ms` | 8000 (como SYSCOM) o 10000 | **10000**: el HTML pesa más que el JSON (la página de acceso pesa 87 KB); ajustable en el admin |
| W8 | ¿Qué es la «sesión» que se guarda como token? | A) Las cookies `ASP.NET_SessionId` y `TS01dc4fc6` serializadas (`nombre=valor; …`) en `NewToken.access_token`. B) Solo `ASP.NET_SessionId` | **A**: el balanceador puede rechazar peticiones sin su cookie. Si la respuesta del login lleva un desafío de Turnstile → `ProviderAuthError` sin `status` y se para: no se evade |
| W9 | ¿Qué se guarda en `ProviderOffer.raw`? | A) El producto completo, como SYSCOM. B) El producto **sin** los datos del agente ni de la localidad de CCONOR | **B**: `raw` llega a la sesión del comparador y no debe llevar datos personales de terceros |
| W10 | ¿De dónde sale el precio? | A) El `precio` de `buscarJSON` (una sola petición). B) El `precio` de `buscarJSONprice`, pedido solo para los primeros `limit` productos de la búsqueda, en una sola petición por lotes | **B**: A es el precio de lista (≈1.82 × el de CCONOR) y saldría caro en el comparador. Una búsqueda = `buscarJSON` + `buscarJSONprice`, en secuencia y dentro del mismo `timeout_ms`. Un producto sin precio en la segunda respuesta se descarta |
| W11 | `sku` (P4) | A) `buscar=<sku>` y quedarse solo con los de `codigo_proveedor` idéntico (sin distinguir mayúsculas). B) Todos los «similares» | **A**: el comparador empareja por clave exacta; los «similares» son otros productos |
| W12 | Muestras de prueba (P9) | A) Escritas a mano. B) Generadas desde las respuestas reales copiando **solo claves y tipos**, con todos los valores ficticios | **B** para el JSON (el generador vive fuera del repo, en el scratchpad; una comprobación confirmó que ningún valor real pasó a `tests/muestras/`: solo coinciden nombres de campos y palabras genéricas). El HTML del acceso, recortado a mano con los nombres reales de los campos |

## 7. Errores y casos límite

| Situación | Resultado |
|---|---|
| Faltan `PROVIDER_EXEL_USUARIO` / `_CONTRASENA` | `ProviderConfigError` del núcleo, sin peticiones |
| El sitio rechaza el login | `ProviderAuthError` **sin `status`**, sin reintento |
| Sesión caducada durante una búsqueda | `ProviderAuthError(status=401)` → el núcleo invalida, inicia sesión una vez y reintenta |
| 403, captcha o página de bloqueo | Error; no se reintenta ni se evade |
| 429 | `ProviderRateLimitError`, sin esperar ni reintentar |
| Estructura inesperada | `ProviderResponseError("La estructura del sitio cambió.")` |
| Página de «sin resultados» | `ok`, 0 ofertas |
| Producto sin nombre, sin precio o con precio 0 | Se descarta y se cuenta |

Los mensajes nunca llevan la cookie, el usuario, la contraseña ni el HTML recibido.

## 8. Plan de pruebas (Fase 4)

Sin red y sin credenciales reales: `ApiFalsa`, `sin_red()`, `credenciales_de_prueba("exel", …)`, `exigir_bd_de_pruebas()` y los centinelas de `apps/providers/tests/soporte.py` (solo funciones y constantes). Las muestras HTML se sirven como bytes con `Content-Type: text/html; charset=utf-8`.

| Bloque | Casos |
|---|---|
| Extracción | Etiquetas anidadas o mal cerradas, entidades HTML, espacios |
| `request_token` | Formulario exacto (incluido el CSRF) · login rechazado → sin `status` y sin la contraseña en el mensaje · respuesta sin cookie → error |
| `fetch` | Parámetros · `sku` manda sobre `term` · recorte del término · «sin resultados» → `[]` · estructura cambiada → error · redirección a login → 401 · `base_url` con barra final |
| `parse_offer` | Precio con formato a `Decimal` exacto · 0 descartado · moneda · `mpn` · `stock` · `raw` · foto y ficha solo `https` de `exel.com.mx` |
| `search_all` | Búsqueda completa con descartes · una sola sesión en dos búsquedas · sesión caducada renovada una vez · login rechazado = un solo POST · ni la cookie ni la contraseña en logs |
| Registro y reglas | `exel` en `registered_codes()` · G2, G4, G10 |

## 9. Presupuesto del reconocimiento (Fase 3) — APROBADO (2026-10-27)

Hasta **7 peticiones** a `www.exel.com.mx`, en secuencia y con pausas, deteniéndose ante el primer 401/403/429/5xx o un captcha. Avance: **1, 2 y 5 hechas** (§10). Las 3 y 4 no se gastaron, porque sin sesión solo darían la misma redirección: se harán con sesión junto con la 6 y la 7.

| # | Petición | Para qué |
|---|---|---|
| 1 | `GET /robots.txt` | Respetar lo que el sitio permite |
| 2 | `GET` de la tienda o de la búsqueda «camara ip» | Estructura de resultados, plataforma, ¿HTML o JSON? |
| 3 | Búsqueda sin resultados | Marcador exacto de «sin resultados» |
| 4 | Búsqueda por un modelo concreto | ¿Existe búsqueda exacta? |
| 5 | 1 extra autorizada | Lo que quede abierto |
| 6 | Login (GET del formulario + POST) | Solo con credenciales en `.env`: CSRF, cookie, redirecciones |
| 7 | Búsqueda con sesión | ¿Cambian los precios con sesión? |

El HTML crudo se guarda en el scratchpad, fuera del repo. Al repo solo llegan muestras con estructura real y valores ficticios. Las peticiones 6 y 7 esperan a que el usuario ponga las credenciales en `.env`.

## 10. Registro de llamadas reales

| Fecha | Fase | Petición | Resultado |
|---|---|---|---|
| 2026-10-27 | 3 (petición 1) | `GET /robots.txt` | 200 `text/plain`: solo `Disallow: /Contenido/*` |
| 2026-10-27 | 3 (petición 2) | `GET /xlstore/` | 302 → `/xlstore/Acceso`; cookies `ASP.NET_SessionId` y `TS01dc4fc6` (solo se anotaron los nombres) |
| 2026-10-27 | 3 (petición 5, extra) | `GET /xlstore/Acceso` | 200 `text/html; charset=utf-8`, 87 KB: formulario de login WebForms y Turnstile inactivo (§4) |

| 2026-10-27 | 3 (petición 6) | `GET /xlstore/Acceso` + `POST /xlstore/Acceso` (login, una sola vez) | 200 con la misma página de acceso, sin redirección ni cookies nuevas, y el mensaje del servidor «Usuario y/o contraseña incorrectos. Por favor verifique sus datos e intente de nuevo. (1938991)». **Parada:** no se reintenta, para no bloquear la cuenta |
| 2026-10-27 | 3 (petición 6, reintento autorizado tras corregir el usuario en `.env`) | `GET` + `POST /xlstore/Acceso` | Igual: 200, «Usuario y/o contraseña incorrectos… (1939031)», `hdnerror` con 1 carácter. **Parada**: dos rechazos seguidos; no habrá un tercer intento hasta revisar `Master.js` y confirmar el login en el navegador |
| 2026-10-27 | 3 (petición 6, tercer intento autorizado tras corregir la contraseña) | `GET` + `POST /xlstore/Acceso` | **Login correcto:** 302 → `https://www.exel.com.mx/xlstore/`, sin cookies nuevas (la sesión es la misma `ASP.NET_SessionId` del GET, ya autenticada) |
| 2026-10-27 | 3 (petición 7) | `GET /xlstore/Productos/buscar.aspx?busqueda=camara%20ip` con sesión | 200, 906 KB de HTML **sin los resultados**: solo las 71 tarjetas del menú (promociones y destacados, con `costo-detalle` vacío), esqueletos de carga (`shimmer-item`), `listaProductos` vacío y un `#no-resultados` oculto. Los resultados se cargan después con JavaScript (`Scripts/buscar.js`, no descargado). Peticiones 3 y 4 **no hechas**: devolverían la misma página vacía |
| 2026-10-27 | 3 (petición 3, reasignada con autorización) | `GET /xlstore/Scripts/buscar.js` sin sesión | 200 `application/javascript`. Búsqueda en dos pasos con JSON: `Productos/buscarJSON.aspx?buscar=<texto>&borrarExistencia=0&ordenarPor=orden inicial&id_categoria=0&id_subcategoria=0&id_marca=&pruebas=false` → `{"Table": [...], "Table1"…"Table9"}`, y `Productos/buscarJSONprice.aspx?listaproductos=<id>|<id>…` (precios por lote). Campos del producto según el JS: `id_producto`, `descripcion`, `Marca`, `codigo_proveedor`, `precio`, `Existencia`, `ExistenciaMX`, `ExistenciaNacional`, `TieneExistencia`, `Categoria`, `origen`, `tieneFoto`… Sesión caducada → mensaje `sin_usuario`/`sin_cliente`/`logout` |
| 2026-10-27 | 3 (petición 4, reasignada con autorización) | `GET` + `POST /xlstore/Acceso` (login) | Se hizo el login, pero el script falló al imprimir la línea de estado (error de codificación de la consola, `-I` ignora `PYTHONIOENCODING`) **antes** de llamar a `buscarJSON`. Resultado del login sin registrar. La búsqueda **no se hizo** |
| 2026-10-27 | 3 (petición 4, repetida con autorización: un login de más por el fallo anterior) | Login + `GET Productos/buscarJSON.aspx?buscar=camara%20ip&borrarExistencia=0&ordenarPor=orden%20inicial&id_categoria=0&id_subcategoria=0&id_marca=&pruebas=false` | Login 302. Búsqueda 200: **JSON válido anunciado como `text/html`**, 615 KB, sin BOM. `{"Table": 201 productos, "Table1"…"Table9": facetas (marcas, familias, categorías, atributos, segmentación)}`. Todos en una respuesta, sin paginación. Detalle en §4 |

| 2026-10-27 | 3 (presupuesto final aprobado) | Login + `buscarJSONprice` (8 ids de la búsqueda anterior) + `buscarJSON` sin resultados + `buscarJSON` por un `codigo_proveedor` | Login 302; los tres 200 con JSON anunciado como `text/html` (19 KB, 140 B y 4 KB). Detalle en §4 |

| 2026-10-27 | 5 (verificación, presupuesto aprobado: 6 a Exel + 2 a SYSCOM) | Alta de `Provider exel` (`timeout_ms` 10000, activo) desde `manage.py shell` y dos `search_all(ProductQuery(term="camara ip", limit=3))` seguidas, contando las peticiones con un transporte que delega en el real | Las dos `ok`: 3 ofertas de Exel cada una (3526 ms y 2251 ms), 0 descartadas, precio `Decimal` > 0 en MXN, `model` = `mpn` (`DH-IPC-HDW3241T-ZAS`, `IPC-T22EN`, `CAM-500`), existencias, foto y ficha; `raw` con 19 campos permitidos y su `precios`. Peticiones a Exel: 1 GET + 1 POST de `Acceso` (**una sola sesión**, reutilizada en la segunda búsqueda), 2 `buscarJSON` y 2 `buscarJSONprice`. SYSCOM: 2 GET y 0 tokens nuevos. Una fila nueva en `ProviderToken` (la sesión de Exel). Sin errores, 429 ni desafíos |

**Total del reconocimiento: 21 peticiones HTTP**: `robots.txt`, `/xlstore/`, `Acceso`, **6 logins** (GET + POST cada uno: 2 rechazados por credenciales, 1 correcto, 1 cortado por el fallo del script, 2 correctos), la búsqueda HTML, `buscar.js`, 3 `buscarJSON` y 1 `buscarJSONprice`. **Fase 3 cerrada.** Pendiente fuera de presupuesto: cómo responde `buscarJSON` con la sesión caducada (se diseña para 302 a `Acceso` o un cuerpo no JSON con `sin_usuario`/`sin_cliente`/`logout`, según `buscar.js`). Todas con autorización, en secuencia y con pausas. Sin 429, bloqueos ni desafíos.

Sin 429 ni desafíos de Turnstile. Tras el rechazo del login, las peticiones 7, 3 y 4 quedan pendientes. Respuesta ante un login rechazado: 200 con la página de acceso y el texto «Usuario y/o contraseña incorrectos», que el scraper traducirá a `ProviderAuthError` sin `status` (P12). El HTML crudo se guardó solo en el scratchpad de la sesión, fuera del repo. Sin cookies, valores ocultos ni credenciales en el repositorio.
