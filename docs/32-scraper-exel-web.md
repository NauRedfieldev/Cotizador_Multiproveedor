# 32 · Scraper de Exel del Norte (M9): cómo funciona y cómo se conecta

| | |
|---|---|
| **Fuente de verdad** | [`apps/providers_web/`](../apps/providers_web/) y, sobre todo, [`scrapers/exel.py`](../apps/providers_web/scrapers/exel.py). Si este documento y el código discrepan, manda el código |
| **Diseño y decisiones** | [31-sdd-m9-exel-web.md](31-sdd-m9-exel-web.md): ficha del sitio (§4), decisiones P1–P16 y W1–W12 (§6) y registro de peticiones reales (§10) |
| **Estado** | Implementado y verificado contra el sitio real el 2026-10-27: 41 pruebas (E1–E28, X1–X6), 100 % de líneas y ramas, 43 de 43 mutaciones detectadas |
| **Se apoya en** | El contrato y la orquestación de M2 ([14](14-contrato-y-orquestacion.md)), sin cambios. Sigue el estándar de [20](20-guia-integrar-api-de-proveedor.md), adaptado a un sitio web; el ejemplo de referencia es SYSCOM ([17](17-adaptador-syscom.md)) |

## 1. Para qué sirve

Exel del Norte no tiene API. Con M9, `search_all` consulta su tienda web con la cuenta de cliente de CCONOR y devuelve sus productos como `ProviderOffer`, igual que con SYSCOM:
- precio de la cuenta de CCONOR en `Decimal`, **en MXN y sin IVA**;
- marca, y el código del fabricante como modelo y MPN, para el emparejamiento del catálogo;
- existencias, foto y ficha.

## 2. Dos vías de datos separadas

| | `apps/providers` | `apps/providers_web` |
|---|---|---|
| Para | Proveedores **con API** | Proveedores **sin API** (web scraping) |
| Cada proveedor | `adapters/<code>.py` | `scrapers/<code>.py` |
| Núcleo | M2: contratos, registro, tokens, `search_all`, modelos | Nada propio: reutiliza M2 |
| Dependencias | No importa de `providers_web` (regla G10) | Solo de `providers` (G4: nunca de `catalog` ni `quotes`) |

`apps/providers_web` es una app Django sin modelos ni migraciones. Su `ProvidersWebConfig.ready()` importa `scrapers`, y cada scraper se registra con `@register` de `apps.providers.registry`: para `search_all` es un adaptador más.

## 3. Piezas

| Archivo | Qué contiene |
|---|---|
| `apps/providers_web/apps.py` | `ProvidersWebConfig` (en `INSTALLED_APPS` como `'apps.providers_web'`) |
| `apps/providers_web/base.py` | `WebScraperAdapter(ProviderAdapter)`: URL de la tienda, cabeceras de navegador (`CABECERAS_NAVEGADOR`) y de las llamadas AJAX (`CABECERAS_AJAX`), cabecera `Cookie` explícita. `unir_cookies(*respuestas)` y `es_redireccion(respuesta)` |
| `apps/providers_web/extraccion.py` | `campos_de_formulario(html, form_id)`: lo que un navegador enviaría con un formulario (con `html.parser`, sin dependencias nuevas) |
| `apps/providers_web/scrapers/exel.py` | `ExelScraper` (`code="exel"`) y sus constantes |
| `apps/providers_web/tests/` | `test_scraper_exel.py` (E1–E28), `test_extraccion.py` (X1–X6) y `muestras/exel_*` (estructura real, valores ficticios) |

## 4. Cómo funciona una búsqueda

```
await search_all(ProductQuery(term="camara ip"))      # o ProductQuery(sku="DH-IPC-HDW3241T-ZAS")
  └─ Provider "exel" activo → registry → ExelScraper(provider, credenciales de .env)
       └─ fetch
            ├─ sesión: la de ProviderToken (20 min) o, si no hay, request_token:
            │    GET  {base_url}/Acceso   → formulario Form1 + cookies
            │    POST {base_url}/Acceso   → 302 a la tienda = sesión abierta
            ├─ GET {base_url}/Productos/buscarJSON.aspx?buscar=…&(parámetros fijos)  → {"Table": [...]}
            │    con sku: solo los de codigo_proveedor idéntico; luego recorte a limit
            ├─ GET {base_url}/Productos/buscarJSONprice.aspx?listaproductos=id|id|…  → precios de CCONOR
            └─ cada producto: solo los campos permitidos + su precio
       └─ parse_offer de cada producto (los inválidos se descartan y se cuentan)
  ◄─ ProviderResult("exel", ofertas, error, elapsed_ms, discarded)
```

Medido en la verificación: unos 3,5 s con inicio de sesión y 2,3 s reutilizándola. `timeout_ms` = 10000 cubre el login y las dos peticiones.

### 4.1 Qué se envía a Exel

| `ProductQuery` | Petición | Detalle |
|---|---|---|
| `term` | `buscar` | Espacios normalizados y recorte a 100 caracteres (`maxlength` del buscador) |
| `sku` | `buscar` | Manda sobre `term`. Exel no tiene búsqueda exacta (devuelve «similares»): solo se conservan los productos cuyo `codigo_proveedor` es igual al `sku`, sin distinguir mayúsculas |
| `limit` | — | Exel devuelve todos los resultados en una respuesta; se recortan a `limit` **antes** de pedir los precios |
| — | `borrarExistencia=0`, `ordenarPor=orden inicial`, `id_categoria=0`, `id_subcategoria=0`, `id_marca=`, `pruebas=false` | Los mismos que pone la página; siempre iguales |

Las dos llamadas llevan la cookie de la sesión y `X-Requested-With: XMLHttpRequest`, como las de la propia página. Las respuestas son JSON anunciado como `text/html`; se leen con `parse_json` (`Decimal`).

### 4.2 De un producto de Exel a `ProviderOffer`

| `ProviderOffer` | Campo de Exel | Regla |
|---|---|---|
| `external_id` | `id_producto` | Código interno de Exel, sin espacios alrededor; si no es texto, se descarta |
| `name` | `descripcion` | El texto que pinta la tienda; vacío → se descarta |
| `price` | `buscarJSONprice` → `precio` | **El de la cuenta de CCONOR.** El `precio` de la búsqueda es el de lista (≈ 1,82 ×) y no se usa. Sin precio de CCONOR, inválido o 0 → se descarta |
| `currency` | — | `"MXN"` (confirmado en el portal) |
| `brand` | `Marca` | Sin espacios alrededor; si no hay, `None` |
| `model` y `mpn` | `codigo_proveedor` | El mismo valor en los dos: es el SKU del fabricante (confirmado por el usuario) |
| `stock` | `ExistenciaNacional` | El total nacional; entero ≥ 0 o `None` (no descarta) |
| `image_url` | `tieneFoto` = 1 | `https://contenidos.exel.com.mx/imgProducto/<id_producto>.png`, solo con ids `[A-Za-z0-9._-]` |
| `product_url` | `id_producto` | `https://www.exel.com.mx/xlstore/Productos/Detalle/<id_producto>`. **Pide la sesión de Exel** en el navegador del asesor |
| `raw` | lista blanca | Solo `CAMPOS_PRODUCTO` más `precios` (`precio`, `precio_lista`, `precio_normal`, `origen`, `vigencia`). **Nunca** los datos del agente de ventas de Exel ni la localidad de CCONOR, que vienen en cada producto (W9) |

## 5. Sesión y credenciales

**Credenciales:**
- en `.env` como `PROVIDER_EXEL_USUARIO` y `PROVIDER_EXEL_CONTRASENA` (patrón `PROVIDER_<CODE>_<CLAVE>`; en `.env_example` solo los nombres);
- si faltan, el núcleo devuelve `ProviderConfigError` sin ninguna petición;
- nunca aparecen en logs ni en mensajes (prueba E27 con centinelas).

**Sesión (W3, W8):**
- `request_token` hace el login de WebForms: los campos ocultos de `Form1` (`__VIEWSTATE`, `__EVENTVALIDATION`…), el usuario, la contraseña y el botón `Iniciar Sesión`;
- el «token» son las cookies de la sesión (`ASP.NET_SessionId`, `TS01dc4fc6`) como cabecera `Cookie`, con una vida de 20 min (`VIDA_SESION`). El núcleo la guarda en `ProviderToken` y la renueva 5 min antes;
- si la sesión caduca antes (una redirección, `sin_usuario`/`sin_cliente`/`logout` o la página de acceso), el scraper lanza `ProviderAuthError(status=401)`: el núcleo la invalida, entra de nuevo y reintenta **una sola vez**.

**Login rechazado** (200 con «Usuario y/o contraseña incorrectos» o 302 de vuelta a `Acceso`): `ProviderAuthError` **sin `status`**, así que no se repite. Repetir logins fallidos puede bloquear la cuenta de CCONOR.

## 6. Errores: qué recibe quien consume

| Situación | `ProviderResult.error` |
|---|---|
| Faltan las credenciales | `ProviderConfigError("Faltan credenciales: PROVIDER_EXEL_USUARIO, …")` |
| Exel rechaza el login | `ProviderAuthError("Exel rechazó el inicio de sesión.")`, sin reintento |
| Sesión caducada | Ninguno: el núcleo renueva y reintenta una vez. Si sigue caducada, `ProviderAuthError(401)` |
| 403 | `ProviderAuthError(status=403)` |
| 429 | `ProviderRateLimitError(retry_after=…)`, sin reintento |
| Formulario o respuesta con otra forma | `ProviderResponseError("La estructura del sitio cambió (…).")` |
| 404, 5xx, JSON inválido | `ProviderResponseError` |
| Más de `timeout_ms` | `ProviderTimeout` |
| Búsqueda sin resultados, o `sku` sin coincidencia exacta | Ninguno: `ok`, con 0 ofertas |

## 7. Cómo se conecta con los demás componentes

### 7.1 Con M2 (`apps/providers`)
`ExelScraper` hereda de `ProviderAdapter` (a través de `WebScraperAdapter`), usa `parse_json`, `parse_price`, `raise_for_status` y `NewToken`, y se registra con `@register`. No modifica nada de M2. El `Provider "exel"` y su sesión (`ProviderToken`) viven en las tablas de `providers`. `RawProviderProduct` no se usa (P3).

### 7.2 Con `catalog` (emparejamiento)
`model` = `mpn` = `codigo_proveedor`, el SKU del fabricante, como el `modelo` de SYSCOM: los dos proveedores pueden emparejarse por «MPN exacto» o por «marca y modelo exactos». La confianza en esos identificadores está declarada para `exel` igual que para `syscom` en `CONFIANZA_POR_PROVEEDOR` de `apps/quotes/views.py`: `IdentificadoresConfiables(mpn_confirmado=True, modelo_confirmado=True)` (2026-10-27; prueba `test_v10c_exel_declara_la_misma_confianza_que_syscom`, que falla si se quita la declaración). Sin ella, las ofertas de Exel caerían al fallback de M5.

### 7.3 Con `quotes`
- **Moneda:** MXN; no necesita tipo de cambio.
- **IVA:** precios sin IVA. `CCONOR_PROVIDER_PRICE_BASES['exel'] = {'vat_basis': 'NET'}` (`settings/settings.py`). Según docs/30 A7, eso es lo que hace falta para que añadir una oferta de Exel al cotizador no responda 409. No se ha probado aún desde el cotizador. `quotes` aplica el IVA una sola vez.
- **Precio:** el de la cuenta de CCONOR, nunca el de lista.

### 7.4 Con el frontend
El mock de `static/js/api.js` usa el código `'ct'` con la etiqueta «Exel del Norte». El código real es `exel`, y alinearlo queda fuera de M9.

## 8. Puesta en marcha en otro entorno (equipo)

1. En `.env`, rellenar `PROVIDER_EXEL_USUARIO` y `PROVIDER_EXEL_CONTRASENA` con la cuenta de cliente de CCONOR en la tienda de Exel. **Nunca** van en git.
2. Comprobar el usuario y la contraseña entrando a mano en `https://www.exel.com.mx/xlstore/Acceso`. Un login automático rechazado no se reintenta, pero varios seguidos pueden bloquear la cuenta.
3. Dar de alta el proveedor en el admin (**Providers → Add**):
   - `code`: `exel`;
   - `name`: `Exel del Norte`;
   - `base_url`: `https://www.exel.com.mx/xlstore/`;
   - `timeout_ms`: 10000;
   - `active`: solo cuando ya estén las credenciales.
4. La primera búsqueda abre la sesión y la guarda. No hace falta tocar `ProviderToken`.

## 9. Cómo probarlo

```powershell
python manage.py test apps.providers_web      # E1–E28 y X1–X6, sin red
python manage.py test                          # todo, incluidas G1–G10
```

Red de seguridad: `sin_red()`, `credenciales_de_prueba("exel")` y `ApiFalsa` de `apps/providers/tests/soporte.py`. Las muestras se generaron copiando **solo claves y tipos** de las respuestas reales, con todos los valores ficticios (W12).

## 10. Límites conocidos y pendientes

- **El sitio puede cambiar.** Si cambian el formulario o los servicios JSON, el resultado lleva `ProviderResponseError("La estructura del sitio cambió …")`: nunca una lista vacía silenciosa.
- **Cloudflare Turnstile** está cargado en la página de acceso pero hoy inactivo. Si Exel lo activa, el login automático deja de funcionar (se verá como login rechazado) y **no se evade**: habrá que hablar con Exel.
- **Una búsqueda descarga todos los resultados** (~615 KB para «camara ip»), porque Exel no pagina. Funciona dentro de `timeout_ms`, pero es lo más pesado de la consulta.
- **Respuesta con la sesión caducada:** el diseño sigue lo que espera el JavaScript del propio sitio (`Scripts/buscar.js`). No se ha observado en real, para no gastar peticiones.
- **Cotizador:** falta probar de extremo a extremo que una oferta de Exel se añade a una cotización (§7.3).
- **Promociones y precios por volumen** (`origen`, escalas): `precio` ya trae el precio vigente de la cuenta. Las escalas no se usan y solo `origen` y `vigencia` quedan en `raw`.
