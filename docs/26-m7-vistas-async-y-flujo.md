# M7: vista async de cotización y flujo en modo degradado

| | |
|---|---|
| **Código** | `apps/quotes/views.py` (`cotizar_productos` y funciones privadas), `apps/quotes/urls.py` |
| **Pruebas** | `apps/quotes/tests/test_vistas_cotizacion.py` (V1–V14, 16 pruebas, sin red) |
| **Se apoya en** | M2 `search_all` ([14](14-contrato-y-orquestacion.md)), M5 `aemparejar_con_fallback` ([21](21-catalogo-canonico-y-m5b.md), [22](22-m5c-fallback-y-guardas.md)), M6 `comparar_configurado` ([23](23-m6-comparacion-y-moneda.md)) |
| **Lo pinta** | M7b: la plantilla `comparador.html`, con el contexto de la §4 (entrega detallada en [27](27-entrega-m7b-comparador-y-cotizador.md)) |

## 1. Qué resuelve

Es el criterio central del proyecto: **si se corta un proveedor, la cotización sale igual, en modo degradado**.
La vista consulta a todos los proveedores activos en paralelo, compara los precios en MXN, añade la
identidad canónica de cada oferta y responde:

- **200** aunque caigan proveedores: los caídos llegan en `proveedores_caidos` para mostrar un aviso;
- **503** solo cuando no queda ningún proveedor útil;
- **nunca un 500** por culpa de un proveedor.

## 2. Ruta y parámetros

`GET /cotizaciones/buscar/` (`name="cotizar_productos"`). Solo GET: cualquier otro método recibe un 405.

| Parámetro | Obligatorio | Uso |
|---|---|---|
| `q` | `q` o `sku` | Texto de búsqueda (`ProductQuery.term`) |
| `sku` | `q` o `sku` | Modelo exacto. Si llega, manda sobre `q` (SYSCOM: `?modelo=`) |
| `categoria` | No | Id **interno** de `catalog.Category`, entero > 0. Solo habilita el fallback M5c |
| `tipo_cambio` | Si hay ofertas en USD | Pesos por dólar con punto decimal (`17.50`). Se captura a mano en cada búsqueda (docs/23) |

## 3. Flujo

```
GET /cotizaciones/buscar/
 1. rate limit por IP ─────────────────────────► 429 (antes de cualquier I/O)
 2. validar q/sku, categoria, tipo_cambio ─────► 400 (sin consultar a nadie)
 3. M2  resultados = await search_all(consulta)        (paralelo, timeouts, token)
 4.     disponibles = r.ok ; caidos = r.provider_code de los que fallaron
        sin disponibles (o sin proveedores activos) ► 503
        ofertas USD y sin tipo_cambio ───────────► 400 "Captura el tipo de cambio"
 5. M6  comparar_configurado([r], tipo_cambio) por proveedor
        si M6 rechaza los datos de uno, ese pasa a caidos (se registra solo el tipo de error)
        si no queda ninguno ─────────────────────► 503
 6. M5  aemparejar_con_fallback(oferta, categoria_id, confianza) con asyncio.gather,
        solo sobre las ofertas que se muestran (OFERTAS_POR_PROVEEDOR por proveedor)
 7. render comparador.html ────────────────────► 200
```

- **Comparación por proveedor.** M6 agrupa por proveedor, así que llamarlo una vez por proveedor da el
  mismo ranking que una sola llamada con todos. La diferencia es que unos datos incoherentes de un
  proveedor (p. ej. un `external_id` duplicado) solo lo dejan fuera a él.
- **Emparejamiento después del ranking.** M5 no cambia el precio ni el orden: solo añade la identidad.
  Por eso se aplica únicamente a las ofertas visibles (3 por proveedor por defecto), lo que limita las
  consultas a la BD.
- **ORM.** La vista solo usa ORM async para leer los nombres de proveedor y las categorías del contexto (2 consultas por petición). M5 usa ORM síncrono envuelto en
  `sync_to_async(thread_sensitive=True)` (decisión del equipo, docs/22). El render también va por
  `sync_to_async`, porque los context processors pueden leer la sesión.

## 4. Contrato de contexto para M7b

Todas las respuestas (200, 400, 429 y 503) renderizan `comparador.html` con las mismas claves:

| Clave | Tipo | Contenido |
|---|---|---|
| `ofertas` | `list[OfertaPresentada]` | Ordenada por `precio_comparable`, luego `provider_code` y `external_id`. Vacía en los errores |
| `proveedores_caidos` | `list[str]` | `code` de los proveedores caídos o descartados por M6, en orden alfabético |
| `proveedores_caidos_nombres` | `list[str]` | `Provider.name` de cada caído, en el mismo orden (para el aviso) |
| `tipo_cambio` | `TipoCambio \| None` | `valor` (Decimal, MXN por USD), `origen` (`"manual"`) y `fecha` (captura, con zona horaria) |
| `categorias` | `list[Category]` | Categorías activas, ordenadas por nombre, para el `<select>` del formulario (`c.pk`, `c.name`) |
| `error` | `str` | Solo en 400, 429 y 503. Texto para el usuario, sin trazas ni datos internos |

`OfertaPresentada` (dataclass congelada, en `apps/quotes/views.py`):

| Campo | Contenido |
|---|---|
| `oferta` | `ProviderOffer` original: `provider_code`, `external_id`, `name`, `price`, `currency`, `brand`, `model`, `mpn` y `stock` |
| `precio_comparable` | `Decimal` en MXN, sin cuantizar. Para mostrarlo, redondear en la plantilla |
| `es_mejor_precio` | `True` si es el precio más bajo **de su proveedor** (M6) |
| `match` | `ResultadoMatch` de M5: `canonico`, `estado` (`AUTO_MATCH`, `REVISION_MANUAL` o `SIN_MATCH`), `metodo`, `score`, `motivo` y `candidatos` |
| `proveedor_nombre` | `Provider.name` (o el `code` si no se encuentra). Va resuelto porque las plantillas no indexan un diccionario con una variable |

Lo que la plantilla debe mostrar (docs/23): `stock` `0` y `None` son distintos (`None` = desconocido), y
el mejor precio no promete disponibilidad. Una sugerencia `REVISION_MANUAL` no es una identidad confirmada.

## 5. Confianza por proveedor (M5)

M5 no autoriza ninguna clave por defecto: el consumidor lo declara. La vista lo hace en
`CONFIANZA_POR_PROVEEDOR`:

| Proveedor | Declaración | Respaldo |
|---|---|---|
| `syscom` | `mpn_confirmado=True`, `modelo_confirmado=True` | docs/16 D11: el `modelo` de SYSCOM es el número de parte del fabricante, verificado con la API real |
| Cualquier otro | `IdentificadoresConfiables()` (ninguna clave) | Hasta que se verifique su significado. Solo podrá obtener sugerencias fuzzy con `categoria` |

Al integrar un proveedor nuevo (docs/20), su declaración se añade aquí junto con su respaldo.

## 6. Rate limiting

- 10 búsquedas por IP (`REMOTE_ADDR`) cada 60 s (`LIMITE_COTIZACIONES`, `VENTANA_SEGUNDOS`).
- Usa la caché de Django con su API async (`aadd` y `aincr`), sin dependencias nuevas. Si la clave caduca
  entre las dos llamadas, cuenta como la primera petición.
- El límite se comprueba antes de consultar a nadie, así que también protege los límites de peticiones de
  SYSCOM.
- **Limitación:** no hay `CACHES` configurado, así que se usa la `LocMemCache`, que cuenta por proceso. Con
  varios workers de Uvicorn, el límite real se multiplica por el número de workers.

## 7. Pruebas

`apps/quotes/tests/test_vistas_cotizacion.py`, con `AsyncClient` y etiqueta `django_db`. Los proveedores
son adaptadores de prueba (`crear_adaptador` y `adaptadores_registrados` de
`apps/providers/tests/soporte.py`) cuyo `fetch` no hace HTTP, y `sin_red()` corta cualquier intento de
salir a la red.

| # | Caso | Esperado |
|---|---|---|
| V1 | Un proveedor caído (timeout) | 200, ofertas del vivo y el caído en el aviso |
| V2 | Ambos caídos | 503 con `comparador.html` y sin traza |
| V3 | Ambos vivos | 200, ordenadas por precio comparable |
| V4 | USD sin y con `tipo_cambio` | 400 / 200 con la conversión exacta (`10.25 × 17.50 = 179.3750`) |
| V5 | Consulta vacía, `tipo_cambio` o `categoria` inválidos | 400 sin consultar proveedores |
| V6 | Ningún proveedor activo | 503 |
| V7, V7b | Rate limit y clave caducada entre `aadd` y `aincr` | La 11.ª da 429 sin consultar; la caducada cuenta 1 |
| V8 | Excepción inesperada de un adaptador | 200 degradado, sin filtrar el mensaje interno |
| V9 | Datos que M6 rechaza | Solo ese proveedor cae; si era el único útil, 503 |
| V10, V10b | Confianza por proveedor y `categoria` | `syscom` empareja por modelo; otro proveedor no; la categoría llega a M5c |
| V11 | POST | 405 |
| V12 | `OFERTAS_POR_PROVEEDOR=1` | Solo la mejor de cada proveedor |
| V13 | Categorías en el contexto | Solo las activas, ordenadas, también en un 400 |
| V14 | Nombres de proveedor | `proveedor_nombre` y `proveedores_caidos_nombres` legibles |

Comandos (desde la raíz):

```powershell
venv\Scripts\python.exe manage.py test apps.quotes.tests.test_vistas_cotizacion
coverage run --data-file=$env:TEMP\cconor.coverage --branch --source=apps/quotes --omit="apps/quotes/tests/*" manage.py test apps.quotes.tests.test_vistas_cotizacion
coverage report --data-file=$env:TEMP\cconor.coverage -m --include="apps/quotes/views.py"
```

El código de M7 queda al 100 % de líneas y ramas.

## 8. Cómo se conecta

```
comparador.html (M7b) ◄── contexto §4 ── cotizar_productos (M7)
                                            │
            ┌───────────────────────────────┼───────────────────────────────┐
            ▼                               ▼                               ▼
  providers.service.search_all   quotes.comparison.comparar_configurado   catalog.matching
  (M2: adaptadores, tokens,      (M6: MXN, TipoCambio, top-n,             aemparejar_con_fallback
   timeouts, errores como datos)  OFERTAS_POR_PROVEEDOR)                   (M5b + M5c)
```

- **providers:** la vista no conoce los adaptadores. Un proveedor nuevo que se registre y se active entra
  solo en la búsqueda; solo hay que añadir su confianza (§5) si se quiere emparejar por clave.
- **catalog:** solo lectura (`ProductoCanonico`, `Category` y sinónimos). No se guarda ninguna decisión.
- **quotes:** no se crea ni modifica ninguna `Quote`. Persistir la cotización y congelar precio, moneda y
  tipo de cambio queda para M8 (snapshot, docs/23).

## 9. Pendientes

- **Sesión.** El acuerdo de SYSCOM (docs/02 §8.2) recomienda mostrar los precios de distribuidor solo a
  usuarios con sesión. Hoy no se exige, porque no hay flujo de login fuera del admin.
- **Rate limit compartido.** Configurar `CACHES` con un backend común entre procesos y, detrás de un proxy,
  tomar la IP real de una cabecera de confianza (hoy se usa `REMOTE_ADDR`).
- **Tipo de cambio.** Obtenerlo y cachearlo (p. ej. con `GET /tipocambio` de SYSCOM) en lugar de la captura
  manual. Necesita su propio plan y un presupuesto de llamadas reales.
- **Frontend.** `static/js/api.js` espera una API JSON (`/api/v1/products/search/`) y sigue con
  `USE_MOCKS = true`. Esta vista entrega HTML; el puente entre los dos contratos queda por decidir.
- **Plantillas.** `comparador.html` todavía no usa las variables de la §4: lo hace M7b.
- **Numeración.** El SDD de M1 (docs/12) llamaba M7 al PDF y M8 a las vistas. Este documento sigue la
  numeración actual del equipo (docs/23): M7 = integración de vistas, M8 = snapshot.
