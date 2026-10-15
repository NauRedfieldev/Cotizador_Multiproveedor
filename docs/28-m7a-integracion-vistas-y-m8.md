# M7a: vistas async, API JSON, login y cotizaciones sobre M8

| | |
|---|---|
| **Código** | `apps/quotes/views.py` (`cotizar_productos`, `buscar`), `apps/quotes/api.py`, `apps/quotes/urls.py`, `settings/urls.py`, `settings/settings.py`, `templates/registration/login.html` |
| **Pruebas** | `apps/quotes/tests/test_vistas_cotizacion.py` (4 del equipo, V1–V16, P1–P6), `test_api_busqueda.py` (A1–A11), `test_api_cotizaciones.py` (C1–C16) |
| **Refina** | [26](26-m7-vistas-async-y-flujo.md) (vista M7) y [27](27-entrega-m7b-comparador-y-cotizador.md) (entrega M7b). Siguen valiendo salvo lo que cambia aquí |
| **Se apoya en** | M2 `search_all` ([14](14-contrato-y-orquestacion.md)), M5 ([21](21-catalogo-canonico-y-m5b.md), [22](22-m5c-fallback-y-guardas.md)), M6 ([23](23-m6-comparacion-y-moneda.md)), M8 ([24](24-sdd-m8-snapshot-pdf.md), [25](25-m8-snapshot-pdf-integracion.md)) |

> **Numeración.** Los docs de M7 se escribieron como 24 y 25, pero esos números los ocupó M8 en `main`.
> Al integrar se renombraron a 26 y 27 sin cambiar su contenido (solo los enlaces entre ellos).

## 1. Qué resuelve y reparto con M8

[25 · Integración M8](25-m8-snapshot-pdf-integracion.md) §4–§6 dejó a M7 la conexión de los servicios de
M8: endpoints de creación, edición y emisión, login real, CSRF y respuestas HTTP. Este módulo (M7a) lo hace
y, de paso, reintegra la vista async de M7 con lo que el equipo fusionó en `main` (M8 #8, M7b y el
fix 7bf8e93).

| Responsable | Qué |
|---|---|
| **M7a** (este doc) | Vistas async, endpoints, autenticación, CSRF y traducción de errores a HTTP |
| **M8** | Decisiones de CCONOR, cálculo comercial y diseño costo/venta: **margen**, datos de empresa y condiciones |

M7a no modifica `snapshots.py`, `models.py` ni `pdf.py`: llama a los servicios tal como están.

## 2. Reconciliación de la vista con `main` (7bf8e93)

El fix 7bf8e93 rehízo `cotizar_productos` como vista **síncrona** (`async_to_sync(search_all)`). Se volvió a
la versión async conservando lo que aportaba:

| Se conserva de 7bf8e93 | Se recupera de M7 (docs/26) |
|---|---|
| Ruta `/comparador/` (nombres `comparador` y `cotizar_productos`) | Vista `async def`: bajo ASGI una vista síncrona ocupa el hilo compartido de las vistas síncronas, y una búsqueda lenta de proveedores bloquearía las demás |
| Sin `q` ni `sku`: 200 con el formulario en blanco | Rate limit 10/60 s por IP antes de cualquier I/O |
| Las seis claves siempre, con `error=None` en los 200 | M6 **por proveedor**: datos incoherentes de uno solo lo dejan fuera a él (una sola llamada tiraba toda la búsqueda) |
| La categoría debe ser una de las activas del formulario (400 si no) | `CONFIANZA_POR_PROVEEDOR`: SYSCOM empareja por modelo/MPN (sin ella, M5 nunca emparejaba por clave) |
| Fallo inesperado de `search_all` → 503 genérico | USD sin tipo de cambio → 400 mirando las ofertas, no el texto de un error |

Otros cambios:

- **Se quitó** la guarda que atrapaba `DatabaseError` si `ProductoCanonico` no existía y degradaba a
  `SIN_MATCH`: ocultaba una migración pendiente. Con la BD sin migrar, el arreglo es `manage.py migrate`.
- El formulario en blanco **no cuenta** en el rate limit (no consulta a nadie).
- `urls.py`: `comparador` va **antes** que `cotizar_productos`. `resolve()` devuelve el primer nombre y el
  menú de `base.html` lo usa para marcar la página activa; antes nunca se marcaba "Comparador".
- El flujo quedó en `views.buscar(request) → (status, contexto)`, compartido por el HTML y la API JSON.

## 3. Flujo

```
GET /comparador/  ─┐                         ┌─► comparador.html (render con sync_to_async)
                   ├─► login ─► buscar() ────┤
GET /api/v1/products/search/ ─┘              └─► JSON (§4) + ofertas en sesión (§5)

buscar(): sin q/sku → 200 en blanco · rate limit → 429 · categoría/tipo de cambio → 400
          M2 search_all (paralelo) → sin proveedores activos o todos caídos → 503
          USD sin tipo de cambio → 400 · M6 por proveedor · M5 con asyncio.gather → 200
```

Sin sesión, el comparador redirige al login y la API responde 401, en ambos casos **antes** de consultar a
nadie y sin contar en el rate limit.

## 4. API de búsqueda: `GET /api/v1/products/search/`

Mismos parámetros que el comparador (`q`, `sku`, `categoria`, `tipo_cambio`) y el cuerpo de
[27 §B2](27-entrega-m7b-comparador-y-cotizador.md). Diferencias con el comparador:

- Sin `q` ni `sku` responde **400** (`"Escribe qué buscar o el modelo exacto."`), no un formulario en blanco.
- Sin sesión responde **401** `{"success": false, "error": "Inicia sesión para continuar."}`.
- `tipo_cambio.fecha` va en ISO 8601 con desfase (`2026-10-15T18:00:00+00:00`); `new Date()` la lee igual
  que la forma con `Z` del ejemplo de §B2.
- Los importes son texto sin notación científica (`"179.3750"`); `score` es entero o `null`.

## 5. Ofertas en sesión

Cada búsqueda con resultados guarda sus ofertas en `request.session["m7a_ofertas"]`, con la clave
`"<provider_code>:<external_id>"`: `name`, `brand`, `model`, `mpn`, `price` y `currency` **de origen** (texto),
`stock`, `es_mejor_precio`, `provider_name`, el `tipo_cambio` de esa búsqueda y `captured_at`.

- Una búsqueda nueva pisa la oferta repetida; se conservan las 200 más recientes. Una búsqueda con error
  no guarda ni borra nada.
- Las sesiones están en la BD: las ven todos los workers de Uvicorn.
- **Por qué:** al guardar una cotización el precio no puede venir del navegador. M8 congela el precio de
  origen como evidencia (`QuoteSnapshot.source_unit_price`), así que sale de lo que el backend devolvió.
- La frescura (cuánto tiempo vale una oferta vista) la decide M9 ([25](25-m8-snapshot-pdf-integracion.md) §6).

## 6. Login, protección y CSRF

- `django.contrib.auth`: `LoginView` en `/login/` y `LogoutView` en `/logout/` (**solo POST**).
  `LOGIN_URL = 'login'`, `LOGIN_REDIRECT_URL = 'inicio'`, `LOGOUT_REDIRECT_URL = 'login'`. Antes, el
  `login_required` de las vistas de PDF y correo de M8 llevaba a `/accounts/login/`, que no existía.
- Plantilla mínima `templates/registration/login.html` (extiende `base.html`). Los usuarios se crean en el
  admin; no hay registro ni recuperación de contraseña.
- Con sesión: comparador (también por el acuerdo de SYSCOM, docs/02 §8.2: precios de distribuidor solo
  con sesión), cotizador y resumen. `inicio` sigue público. Un usuario inactivo no tiene sesión.
- APIs `/api/v1/…`: `api.sesion_requerida` responde 401 en JSON en lugar de redirigir.
- `/cotizador/` entrega la cookie `csrftoken` (`ensure_csrf_cookie`, igual que `/resumen/`). Los POST/PUT
  de §7 exigen la cabecera `X-CSRFToken`; el rechazo (403) lo da `CsrfViewMiddleware` con su página HTML,
  no en JSON. Como el middleware actúa antes que la vista, un POST anónimo **sin** token recibe 403, no 401.

## 7. Cotizaciones: endpoints sobre M8

Todos son async, exigen sesión y CSRF (los de escritura), y llaman a los servicios de M8 fuera del event
loop. La emisión **no** se envuelve en ninguna transacción de la vista (M8 §2).

| Método y ruta | Servicio M8 | Cuerpo | Éxito |
|---|---|---|---|
| `POST /api/v1/quotes/` | `crear_borrador` (crea antes la `Quote`) | Alta (abajo) | 201 |
| `GET /api/v1/quotes/<folio>/` | `authorize` + lectura | — | 200 |
| `PUT /api/v1/quotes/<folio>/revisiones/<n>/` | `aguardar_borrador` | Alta sin `request_id`, con `edit_version` | 200 |
| `POST /api/v1/quotes/<folio>/revisiones/<n>/emitir/` | `aemitir_revision` | `{"edit_version": 1}` | 200 |
| `POST /api/v1/quotes/<folio>/revisiones/<n>/nueva/` | `acrear_revision` | `{"request_id": "<uuid>"}` | 201 |

Cuerpo del alta (claves desconocidas → 400):

```json
{
  "request_id": "6f1c2a52-6a3b-4c1e-9d6e-2f0f8a1b3c4d",
  "client_name": "Cliente S.A. de C.V.",
  "client_rfc": "CSI980415AA2",
  "client_email": "compras@cliente.example.com",
  "valid_days": 15,
  "notes": "Entrega en sitio.\nPago contra entrega.",
  "items": [{ "provider": "syscom", "external_id": "123456", "quantity": "2", "description": "" }]
}
```

- **Partidas:** solo `provider`, `external_id`, `quantity` (texto, hasta 2 decimales, mínimo `0.01`) y
  `description` opcional. Precio, moneda y tipo de cambio salen de la sesión (§5) y la partida se arma con
  `PartidaCapturada.desde_oferta` de M8. `costo_mxn`, `margen` o `tipo_cambio` en el cuerpo → 400.
- **409 de oferta:** si una partida no está en la sesión ("Vuelve a buscar el producto…"), o si las partidas
  en USD vienen de búsquedas con distinto tipo de cambio (una cotización no los mezcla). Las partidas en MXN
  no dependen del tipo de cambio; con solo MXN no se guarda ninguno.
- **Idempotencia del alta:** el mismo `request_id` devuelve el mismo borrador; con otros datos (cliente,
  vigencia, notas o partidas) es 409. La `Quote` y el borrador se crean en **una** transacción (si M8 rechaza
  el borrador no queda una `Quote` huérfana), con un bloqueo `select_for_update` del usuario para que dos
  altas simultáneas con el mismo `request_id` no creen dos `Quote`. Un choque de folio se reintenta hasta
  3 veces (`Quote._generate_folio` calcula el siguiente folio sin bloqueo).
- **Edición:** reemplazo completo. El cliente capturado sustituye razón social, RFC y correo de la cabecera;
  contacto, teléfono y proyecto, la empresa, la tasa de IVA y la fecha se conservan de la revisión.
- **Respuesta** de alta, edición, emisión y nueva revisión: `{"success": true, "data": {folio, revision,
  status, edit_version, subtotal, tax, total}}`. Los totales solo existen en una emisión (los sella M8); en un
  borrador son `null` y el PDF del borrador (`/cotizaciones/<folio>/pdf/?revision=N`) muestra los de M8.
- **Detalle:** `{folio, status, revisiones: [...], actual: {…, client, conditions, valid_days, issue_date,
  tax_rate, tipo_cambio, items}}`, con importes como texto; `actual` es `null` en una cotización legacy sin
  revisiones.

Errores (cuerpo `{"success": false, "error": "…"}`):

| Situación | HTTP |
|---|---|
| JSON inválido, campo desconocido o con tipo incorrecto, validación de M8 (`ValidationError`) | 400 |
| `TypeError` de M8 | 400 genérico ("Datos de solicitud inválidos.") |
| Sin sesión | 401 |
| Sin acceso (`PermissionDenied`: ni propietario ni staff) | 403 |
| Cotización o revisión inexistente | 404 |
| Método distinto del de la tabla | 405 |
| `RevisionConflict` (versión obsoleta, editar una emisión, `request_id` con otros datos) y oferta fuera de la sesión | 409 |
| `ArchivedPDFError` (original ausente o dañado) | 503 |
| Cualquier otra excepción | 500 genérico, registrada en el log sin datos sensibles |

El traductor (`api._respuesta_de_error`) es propio y no reutiliza el `_document_error` de las vistas de
M8: aquel responde `{error}` sin `success` y con mensajes propios de documentos.

## 8. Pruebas

| Archivo | Casos |
|---|---|
| `test_vistas_cotizacion.py` | 4 del equipo (blanco, 400, 503, seis claves) · V1–V16: degradado, 503, orden, USD, 400 sin consultar (categoría inexistente o inactiva incluida), rate limit, M6 por proveedor, confianza, categoría a M5, 405, fallo del proceso (V8b), blanco sin contar (V15), menú activo (V16) · P1–P6: login |
| `test_api_busqueda.py` | A1–A11: contrato §B2, `AUTO_MATCH` con canónico, 400/429/503, límite compartido, ofertas en sesión, 401 |
| `test_api_cotizaciones.py` | C1–C16: flujo búsqueda → alta, precio y margen rechazados, 409 de oferta y de tipos de cambio, idempotencia, validación, 401/CSRF, permisos, edición con versión, emisión idempotente, nueva revisión, 405, 500 y 503, reintento de folio y **C16 de concurrencia** (`TransactionTestCase` con hilos) |

C16 fuerza el choque con una barrera y folios distintos: con el mismo folio, la restricción `unique` y el
reintento unen las dos altas por casualidad y la prueba no detectaba la falta del bloqueo (se verificó
quitándolo). Todo sin red (`sin_red()`, adaptadores de prueba) y con el PDF en un directorio temporal.

```powershell
venv\Scripts\python.exe manage.py test apps.quotes
coverage run --data-file=$env:TEMP\cconor.coverage --branch --source=apps/quotes --omit="apps/quotes/tests/*" manage.py test apps.quotes.tests.test_vistas_cotizacion apps.quotes.tests.test_api_busqueda apps.quotes.tests.test_api_cotizaciones
coverage report --data-file=$env:TEMP\cconor.coverage -m --include="apps/quotes/api.py"
```

`api.py` y la sección del comparador de `views.py` quedan al 100 % de líneas y ramas.

## 9. Cómo se conecta

```
comparador.html ◄── cotizar_productos ──┐
                                        ├── buscar() ── M2 search_all · M6 comparar_configurado · M5 aemparejar_con_fallback
cotizador (api.js) ◄── /api/v1/products/search/ ──┘          │
        │                                                    └─► sesión: ofertas vistas (§5)
        └──► /api/v1/quotes/… ── PartidaCapturada.desde_oferta(oferta de la sesión)
                                  └─► M8: crear_borrador · aguardar_borrador · aemitir_revision · acrear_revision
                                      (PDF original: /cotizaciones/<folio>/pdf/ de M8)
```

## 10. Entrega a frontend (cotizador)

El cotizador sigue con `USE_MOCKS = true`. Para conectarlo:

1. **Búsqueda:** `cotizador.js` llama a `products.search({ query, tipo_cambio })` y `api.js` lo manda tal cual:
   el backend espera **`q`** (y opcionalmente `sku` y `categoria`). Renombrar `query` → `q`.
2. **401:** sin sesión, `fetch` recibe 401 en JSON. Redirigir a `/login/?next=/cotizador/`.
3. **409:** leer el JSON (hoy `api.js` solo lo hace para 400/429/503). En el alta significa "vuelve a buscar
   el producto" o "tipos de cambio distintos"; en edición o emisión, "recarga la cotización" con
   `GET /api/v1/quotes/<folio>/`. No reintentar a ciegas con otra versión.
4. **Payload de guardado:** quitar `costo_mxn`, `margen` y `tipo_cambio` de primer nivel (el backend responde
   400). Añadir `request_id`: un UUID (`crypto.randomUUID()`) generado **una vez** por cotización y reutilizado
   en cada reintento. `quantity` como texto; el modelo admite decimales (hasta 2), aunque hoy `cotizador.js`
   usa `parseInt` y `min="1"`.
5. **Después de guardar:** conservar `folio`, `revision` y `edit_version`. Editar con `PUT …/revisiones/<n>/`
   y la `edit_version` vigente; cada edición la incrementa.
6. **Emitir** y **Nueva revisión** son acciones distintas de Guardar (M8 §6). **No conectar "Emitir"
   todavía:** sin margen en M8, el PDF emitido sale a precio de costo (§11).
7. **Totales:** los de pantalla son orientativos; los oficiales los calcula M8 al emitir (`subtotal`, `tax`,
   `total` en la respuesta).
8. **CSRF:** `api.js` ya manda `X-CSRFToken` desde la cookie; `/cotizador/` ya la entrega. Un 403 de CSRF
   llega en HTML.
9. **Menú:** falta un enlace a "Iniciar sesión" y un botón "Cerrar sesión" en `base.html`. El logout es por
   POST: `<form method="post" action="{% url 'logout' %}">{% csrf_token %}<button>Cerrar sesión</button></form>`.
10. `USE_MOCKS` es global: para conectar solo la búsqueda y el guardado, conviene un interruptor por servicio.

## 11. Pendientes

| Pendiente | Responsable |
|---|---|
| **Margen y precio de venta:** M8 guarda y emite el costo de origen; hasta que lo defina, emitir genera el PDF a costo | M8 |
| **Datos de la empresa:** provisional `api.EMPRESA` = `pdf.COMPANY` sin `advisor` (su RFC tiene formato de prueba) | M8 |
| **Condiciones:** provisional, cada línea no vacía de `notes` (no se usó `DEFAULT_CONDITIONS`: trae una CLABE de ejemplo) | M8 |
| Frescura de las ofertas en sesión antes de guardar | M9 |
| Frontend: §10 | Frontend |
| `CACHES` compartida entre workers para el rate limit e IP real detrás de un proxy | Backend (docs/26 §9) |
| Tipo de cambio automático en lugar de la captura manual (presupuesto de llamadas reales) | Backend (docs/26 §9) |
| Listado (`GET /api/v1/quotes/`) y borrado que espera `api.js`: no se implementaron (no los pide M8) | Por decidir |
