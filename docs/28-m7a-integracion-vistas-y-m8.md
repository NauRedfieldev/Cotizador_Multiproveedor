# M7a: vistas async, API JSON, login y cotizaciones sobre M8

**Contrato vigente:** §§13–15 (API P4, cotizador P5 y documentos P6) prevalecen
sobre los ejemplos históricos de §§1–12. La conexión es local/QA: no implica migrar
la BD de trabajo, habilitar emisión real ni aceptación comercial.

| | |
|---|---|
| **Código** | `apps/quotes/views.py` (`cotizar_productos`, `buscar`), `apps/quotes/api.py`, `apps/quotes/urls.py`, `settings/urls.py`, `settings/settings.py`, `templates/registration/login.html` |
| **Pruebas** | `apps/quotes/tests/test_vistas_cotizacion.py` (4 del equipo, V1–V16, P1–P6), `test_api_busqueda.py` (A1–A11), `test_api_cotizaciones.py` (C1–C21 + documental P6) |
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

M7a no modifica `snapshots.py`, `models.py` ni `pdf.py`: llama a los servicios tal como están. La respuesta
de M8 a los pendientes de la §11 está en la §12; donde contradiga a una sección anterior, manda la §12.

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
- Menú de `base.html`: "ENTRAR" sin sesión y "SALIR (nombre)" con sesión (formulario POST con CSRF a `/logout/`).
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
| `POST /api/v1/quotes/<folio>/revisiones/<n>/emitir/` | `aemitir_revision` | `{"edit_version": 1}` | 200 (403 mientras la emisión esté bloqueada, §12) |
| `POST /api/v1/quotes/<folio>/revisiones/<n>/nueva/` | `acrear_revision` | `{"request_id": "<uuid>"}` | 201 |

Cuerpo del alta (claves desconocidas → 400):

```json
{
  "request_id": "6f1c2a52-6a3b-4c1e-9d6e-2f0f8a1b3c4d",
  "client_name": "Cliente S.A. de C.V.",
  "client_rfc": "CSI980415AA2",
  "client_email": "compras@cliente.example.com",
  "valid_days": 15,
  "conditions": ["Entrega en sitio.", "Pago contra entrega."],
  "notes": "Nota interna: no va al PDF.",
  "items": [{ "provider": "syscom", "external_id": "123456", "quantity": "2", "description": "" }]
}
```

- **Empresa:** sale de `settings.CCONOR_EMPRESA` (§12). Si no tiene nombre, el alta responde **503** antes
  de crear nada.
- **Condiciones:** `conditions` es obligatoria aunque vaya vacía (`[]`) y se manda tal cual a M8, sin valores
  por defecto. Es lo único que se publica en el PDF.
- **Notas internas:** `notes` es opcional, solo existe en el alta y se guarda en `Quote.notes`. No va al PDF:
  las cotizaciones de M7a siempre tienen revisión y el PDF sale de ella, no del `notes` legacy.

- **Partidas:** solo `provider`, `external_id`, `quantity` (texto, hasta 2 decimales, mínimo `0.01`) y
  `description` opcional. Precio, moneda y tipo de cambio salen de la sesión (§5) y la partida se arma con
  `PartidaCapturada.desde_oferta` de M8. `costo_mxn`, `margen` o `tipo_cambio` en el cuerpo → 400.
- **409 de oferta:** si una partida no está en la sesión ("Vuelve a buscar el producto…"), o si las partidas
  en USD vienen de búsquedas con distinto tipo de cambio (una cotización no los mezcla). Las partidas en MXN
  no dependen del tipo de cambio; con solo MXN no se guarda ninguno.
- **Idempotencia del alta:** el mismo `request_id` devuelve el mismo borrador; con otros datos (cliente,
  vigencia, condiciones, notas o partidas) es 409. `notes` no forma parte de la huella de M8, así que M7a la
  compara aparte. La `Quote` y el borrador se crean en **una** transacción (si M8 rechaza
  el borrador no queda una `Quote` huérfana), con un bloqueo `select_for_update` del usuario para que dos
  altas simultáneas con el mismo `request_id` no creen dos `Quote`. Un choque de folio se reintenta hasta
  3 veces (`Quote._generate_folio` calcula el siguiente folio sin bloqueo).
- **Edición:** reemplazo completo, con el mismo cuerpo que el alta salvo `request_id` y `notes` (que da 400:
  M7a no escribe la `Quote` y no la ignora en silencio). El cliente capturado sustituye razón social, RFC y
  correo de la cabecera; las condiciones se reemplazan; contacto, teléfono y proyecto, la **empresa** (aunque
  cambie `CCONOR_EMPRESA`), la tasa de IVA y la fecha se conservan de la revisión.
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
| Emitir con `COTIZACIONES_EMISION_HABILITADA = False` | 403 |
| Cotización o revisión inexistente | 404 |
| Método distinto del de la tabla | 405 |
| `RevisionConflict` (versión obsoleta, editar una emisión, `request_id` con otros datos) y oferta fuera de la sesión | 409 |
| `ArchivedPDFError` (original ausente o dañado) | 503 |
| Alta sin `CCONOR_EMPRESA` configurada | 503 |
| Cualquier otra excepción | 500 genérico, registrada en el log sin datos sensibles |

El traductor (`api._respuesta_de_error`) es propio y no reutiliza el `_document_error` de las vistas de
M8: aquel responde `{error}` sin `success` y con mensajes propios de documentos.

## 8. Pruebas

| Archivo | Casos |
|---|---|
| `test_vistas_cotizacion.py` | 4 del equipo (blanco, 400, 503, seis claves) · V1–V16: degradado, 503, orden, USD, 400 sin consultar (categoría inexistente o inactiva incluida), rate limit, M6 por proveedor, confianza, categoría a M5, 405, fallo del proceso (V8b), blanco sin contar (V15), menú activo (V16) · P1–P7: login y menú de sesión |
| `test_api_busqueda.py` | A1–A11: contrato §B2, `AUTO_MATCH` con canónico, 400/429/503, límite compartido, ofertas en sesión, 401 |
| `test_api_cotizaciones.py` | C1–C16: flujo búsqueda → alta, precio y margen rechazados, 409 de oferta y de tipos de cambio, idempotencia, validación, 401/CSRF, permisos, edición con versión, emisión idempotente, nueva revisión, 405, 500 y 503, reintento de folio y **C16 de concurrencia** (`TransactionTestCase` con hilos) · C17–C21 (§12): empresa sin configurar, emisión bloqueada, condiciones obligatorias aunque vacías, la edición conserva la empresa, el detalle no expone la nota interna |

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
   400). Separar el textarea "Notas / condiciones" en **"Condiciones para el cliente"** (`conditions`, una por
   línea → lista, siempre presente aunque vacía; es lo que sale en el PDF) y **"Notas internas"** (`notes`, solo
   al crear; nunca se publica). Añadir `request_id`: un UUID (`crypto.randomUUID()`) generado **una vez** por cotización y reutilizado
   en cada reintento. `quantity` como texto; el modelo admite decimales (hasta 2), aunque hoy `cotizador.js`
   usa `parseInt` y `min="1"`.
5. **Después de guardar:** conservar `folio`, `revision` y `edit_version`. Editar con `PUT …/revisiones/<n>/`
   y la `edit_version` vigente; cada edición la incrementa.
6. **Emitir** y **Nueva revisión** son acciones distintas de Guardar (M8 §6). **No conectar "Emitir"
   todavía:** el backend la bloquea con 403 hasta que exista la política de precios (§12).
7. **Totales:** los de pantalla son orientativos; los oficiales los calcula M8 al emitir (`subtotal`, `tax`,
   `total` en la respuesta).
8. **CSRF:** `api.js` ya manda `X-CSRFToken` desde la cookie; `/cotizador/` ya la entrega. Un 403 de CSRF
   llega en HTML.
9. **Menú:** ya lo añadió M7a en `base.html` (último elemento): sin sesión, "ENTRAR" enlaza a `/login/`; con
   sesión, "SALIR (nombre)" es un formulario POST con `{% csrf_token %}` hacia `/logout/`. Usa las clases del
   menú; frontend puede restilizarlo, pero el logout debe seguir siendo POST con CSRF.
10. `USE_MOCKS` es global: para conectar solo la búsqueda y el guardado, conviene un interruptor por servicio.

## 11. Pendientes

| Pendiente | Responsable |
|---|---|
| **Política de precios de venta (margen):** la emisión está bloqueada hasta tenerla (§12) | CCONOR y M8 |
| **Valores oficiales de la empresa** (`CCONOR_EMPRESA_*` en el `.env` de cada despliegue) y **condiciones oficiales** | CCONOR |
| Estructura definitiva de la fuente de datos de la empresa (hoy `.env`, propuesta de M7a) | M8 |
| Guarda propia en `emitir_revision` además del bloqueo HTTP | M8 |
| **Servicio de vista previa de totales** del borrador (§12): M7a lo expondrá cuando exista | M8, luego M7a |
| Frescura de las ofertas en sesión antes de guardar | M9 |
| Frontend: §10 | Frontend |
| `CACHES` compartida entre workers para el rate limit e IP real detrás de un proxy | Backend (docs/26 §9) |
| Tipo de cambio automático en lugar de la captura manual (presupuesto de llamadas reales) | Backend (docs/26 §9) |
| Listado (`GET /api/v1/quotes/`) y borrado que espera `api.js`: no se implementaron (no los pide M8) | Por decidir |

## 12. Respuesta de M8 a los pendientes (2026-10-15)

El desarrollador de M8 revisó las decisiones provisionales de la §11 original. Separa lo técnico (la
estructura, de M7a/M8) de los datos y políticas que debe confirmar CCONOR.

| Punto | Respuesta de M8 | Qué hace M7a |
|---|---|---|
| **Empresa** | `pdf.COMPANY` son datos de ejemplo, no validados. Excluir `advisor` es correcto (M8 lo toma del propietario). Quitar guiones al RFC no lo haría real. Para uso comercial, la empresa debe venir de una fuente del **servidor** validada por CCONOR, nunca de constantes demo ni del frontend. Los ejemplos solo valen en QA, marcados como sintéticos | `settings.CCONOR_EMPRESA` desde `CCONOR_EMPRESA_NAME`, `_RFC`, `_TAGLINE`, `_PHONE` y `_EMAIL` del `.env` (en `.env_example` solo los nombres). Sin nombre, el alta da 503 y no crea nada; no hay respaldo con `pdf.COMPANY`. Las pruebas usan una empresa sintética "(QA)" |
| **Condiciones** | No usar `DEFAULT_CONDITIONS` es correcto. Convertir `notes` en condiciones solo vale si `notes` es exclusivamente texto para el cliente; si lleva observaciones internas, no se deben publicar. M8 acepta una lista explícita, incluso vacía, sin valores predeterminados | Campo `conditions` explícito y obligatorio (puede ir vacío), enviado tal cual. `notes` pasa a ser nota interna del alta y nunca va al PDF |
| **Margen y emisión** | M8 calcula sobre el costo convertido, sin política de margen ni precio de venta separado. No basta con desactivar el botón: hace falta un **control en el backend** que impida emitir una propuesta comercial incompleta. No resolverlo aplicando el 20 % del frontend, ignorando el porcentaje ni sustituyendo el precio original. La política depende de CCONOR | `COTIZACIONES_EMISION_HABILITADA` (por defecto `False`): `…/emitir/` responde **403** antes de llamar a M8. `margen` y `costo_mxn` se siguen rechazando con 400. Búsqueda, selección, autenticación y borradores siguen disponibles |
| **Totales del borrador** | Los `null` persistidos en DRAFT son deliberados y no se rellenan. Pero no hace falta abrir el PDF: `contexto_revision()` ya calcula importes, subtotal, IVA y total. No duplicar la fórmula en M7a ni en JavaScript, y no exponer ese helper (no comprueba permisos): acordar un servicio público de lectura autorizado que lo reutilice sin generar un PDF | Nada todavía. Propuesta para M8: `obtener_vista_previa(folio, *, actor, number=None)` (y su wrapper async) → `{moneda, subtotal, iva, total}` con `authorize` y sin PDF. M7a lo expondría en `GET /api/v1/quotes/<folio>/` con la forma que propuso M8: `{status, totales_sellados, vista_previa: {moneda, subtotal, iva, total}}` |

**Por qué 403 para la emisión bloqueada:** en este cliente 409 significa "recarga y vuelve a intentarlo" y
503, una avería temporal; aquí la operación no está permitida hasta que cambie una política.

**Por qué la edición conserva la empresa:** es un dato capturado en la revisión, igual que hace
`crear_revision` de M8; cambiar `CCONOR_EMPRESA` afecta solo a las cotizaciones nuevas.

## 13. P4 · API comercial v2 y capturas estables

Implementación en `api.py`, `models.py`, `snapshots.py`, `urls.py` y migración
`0004_commercial_api_contexts`. Motor/esquema: [24 §8–9](24-sdd-m8-snapshot-pdf.md).
La versión HTTP sigue `/api/v1/`; `schema_version=2` identifica formato comercial,
no una segunda API. P4 no cambió frontend ni `USE_MOCKS`; su consumidor P5 se describe en §14.

### 13.1 Rutas y autorización

Todas requieren sesión activa, lectura financiera interna y autorización de objeto
cuando exista Quote; operaciones mutables mantienen CSRF. Staff no sustituye la
capacidad financiera. Roles no amplían acceso entre asesores. ORM/servicios síncronos
se ejecutan completos mediante `sync_to_async(..., thread_sensitive=True)`.

| Método y ruta | Contrato |
| --- | --- |
| GET `/api/v1/products/search/?q=...&schema_version=2` | Ofertas internas con `context_ref`/`offer_ref`; guarda captura normalizada, no raw |
| POST `/api/v1/quotes/preview/` | Captura nueva antes de existir Quote; schema_version2, sin request_id/notes; no escribe, reserva folio ni archiva PDF |
| POST `/api/v1/quotes/` | Alta v2 explícita con request_id; recibo y Quote/revisión en misma transacción; 201 |
| GET `/api/v1/quotes/` | P6: listado paginado y búsqueda de folios propios; §15 |
| GET `/api/v1/quotes/<folio>/` | Lista de revisiones/actual; lectura consistente, no reemplaza selección explícita de documento |
| GET `/api/v1/quotes/<folio>/revisiones/<n>/` | Revisión exacta: originales/inputs internos, preview o sellos, constancias y capacidades |
| PUT en la misma ruta | Reemplazo completo de borrador v2 con edit_version; conserva capturas originales |
| POST `.../revisiones/<n>/preview/` | Simula ese reemplazo sin guardar ni aceptar su huella propuesta |
| POST `.../revisiones/<n>/confirmaciones/` | edit_version, state_fingerprint y warnings explícitos; actor/fecha servidor |
| POST `.../revisiones/<n>/emitir/` | edit_version y state_fingerprint guardados; P3 durable; bandera HTTP sigue requerida |
| POST `.../revisiones/<n>/nueva/` | request_id y schema_version2; desde v2 emitida copia snapshots; desde v1 exige partidas comerciales revisadas y parámetros explícitos |
| POST `/api/v1/quotes/fx-contexts/` | request_id y value decimal; exige facultad FX, origen manual/fecha servidor; 201 |
| GET `/api/v1/commercial/capabilities/` | Capacidades efectivas en data; actor_id identifica la sesión actual (P5) |
| GET/PATCH `/api/v1/commercial/permissions/<user_id>/` | Admin comercial: consultar/ajustar cinco facultades de un usuario ya interno; PATCH capability/ enabled booleano |

Rutas estáticas antes de `<folio>`. No GET global de cotizaciones, DELETE, CRUD de
usuarios/grupos ni refresco automático de ofertas. No abrir UserAdmin al asesor.

### 13.2 Alta y campos de partida

Ejemplo sintético de API; referencias deben salir de una búsqueda del mismo usuario:

```json
{
  "schema_version": 2,
  "request_id": "11111111-1111-4111-8111-111111111111",
  "client_name": "Cliente sintético QA",
  "client_rfc": "",
  "client_email": "",
  "conditions": ["Muestra sin validez comercial."],
  "notes": "Nota interna, nunca condición cliente.",
  "issue_date": "2026-10-16",
  "valid_days": 15,
  "tax_rate": "0.16",
  "discount_percent": "0",
  "fx_context_id": null,
  "items": [{
    "line_key": "22222222-2222-4222-8222-222222222222",
    "source": {"type": "API", "context_ref": "33333333-3333-4333-8333-333333333333", "offer_ref": "44444444-4444-4444-8444-444444444444"},
    "quantity": 2,
    "units": {"source_label": "Pieza", "sale_label": "Pieza", "conversion_factor": "1", "confirmed": true},
    "pricing": {"mode": "MARGIN", "margin_percent": "35.00"}
  }]
}
```

- Empresa/asesor siempre del servidor. Contacto/teléfono/proyecto opcionales como
  `client_contact`, `client_phone`, `client_project`. Fecha YYYY-MM-DD estricta.
- Tasas fracción (.16), porcentajes35/10, precios/factores como TEXTO decimal sin
  exponente/separadores. Cantidad/versión enteros estrictos; bool no es entero válido.
- Nuevas capturas: IVA16%, descuento0 y margen35% si mode=MARGIN omite porcentaje.
  Edición exige fecha/vigencia/IVA/descuento/FX explícitos y margen cuando corresponda;
  no usa defaults para reconstruir ajustes previos. Hasta500 líneas.
- `source.type=API`: solo context_ref/offer_ref del servidor. Se rechazan campos
  precio/costo/moneda/proveedor suministrados por navegador; descripción pública sí
  puede editarse. No usar external_id del distribuidor como código cliente automático.
- `source.type=EXISTING`: solo type, resuelve snapshot de esa revisión/line_key;
  permite editar sin sesión/búsqueda. Otra captura requiere NUEVO line_key explícito.
- `source.type=MANUAL`: name obligatorio, code/brand/model opcionales. Campo `cost`
  es null si desconocido o un precio explícito. No inventar moneda para costo ausente.
- Un precio manual tiene amount/currency y vat_basis=NET o INCLUDED. INCLUDED exige
  vat_rate propia; NET no admite vat_rate. Costo0 conocido conserva amount="0" y moneda.
- `pricing.mode=MANUAL` requiere `sale` con ese contrato de precio; no acepta margen.
  MARGIN admite margin_percent, no sale, y exige costo conocido. Ejemplo venta directa:
  `{"mode":"MANUAL","sale":{"amount":"150.00","currency":"MXN","vat_basis":"NET"}}`.
- source_label/sale_label/factor y confirmed=true son explícitos; actor/fecha de
  validación salen de la petición autenticada, no de un confirmed_by del browser.
- Notas internas solo en alta, no en detalle/PDF. conditions es lista externa
  obligatoria, aunque vacía. No copiar secretos/proveedor a texto público: la allowlist
  estructural no es detector de secretos en texto libre.

### 13.3 Captura confiable y base IVA API

`QuoteSelectionContext` es append-only: UUID, actor PROTECT, kind SEARCH/FX,
payload normalizado y created_at. SEARCH guarda offers por offer_ref UUID, con
originales y base IVA, y referencia de FX observado. FX guarda value/source/date.
Sin raw, credenciales ni caché de proceso como única fuente de verdad. Una nueva
búsqueda crea otro contexto: no pisa el precio seleccionado antes. Referencia ajena,
inexistente o fuera de contexto da409, jamás recaptura silenciosa.

La base IVA de origen NO está en ProviderOffer actual. Por eso P4 consulta la
configuración confiable `settings.CCONOR_PROVIDER_PRICE_BASES`, vacía por defecto.
Solo poblarla tras verificar que el contrato aplica a TODAS las ofertas de ese
proveedor: `{"codigo":{"vat_basis":"NET"}}` o INCLUDED con vat_rate decimal.
No se configura NET por suposición: SYSCOM quedó en NET el 2026-10-19 tras verificarlo
(docs/30 B4); Exel sigue sin configurar (B5). Ausencia=UNKNOWN: la búsqueda
puede mostrar la oferta internamente, pero crear/preview comercial la rechaza409.
Si hay variación por producto, no usar una regla global; falta enriquecer el contrato
verificado por oferta antes de habilitar ese proveedor para uso real.

FX elegido por la cabecera es independiente de fechas/FX incidentales de nuevas
búsquedas. Productos de búsquedas distintas pueden compartir el FX elegido sin
conflicto artificial por timestamp. Aplicar/cambiar FX exige capacidad; conservar
exactamente el capturado permite editar cantidad/cabecera sin facultad de cambiarlo.
Si el contexto original ya no estuviera disponible, una línea guardada se reconstruye
de su snapshot. No se añade caducidad comercial, retención o limpieza automática:
esas políticas siguen pendientes de M9/operación antes del uso real.

### 13.4 Recibos e idempotencia

`QuoteAPIRequest`: actor PROTECT + request_id únicos, operation CREATE/CLONE/FX,
SHA-256 de intención y resultado FK a revisión O contexto FX (no ambos), created_at.
Inmutable, sin almacenar el JSON completo del cliente ni notas claras en el recibo.
La huella incluye operación, destino y JSON tipado ordenado por claves, excluyendo
request_id. Reintentar requiere los mismos valores/tipos ("35" y "35.00" no se
normalizan semánticamente como la misma petición). No reformatear el cuerpo al reintentar.

En PostgreSQL se toma advisory lock transaccional por actor+UUID antes de resolver
el recibo, sin bloquear User antes de Quote (evita invertir Quote→User de P3).
Recibo/resultado se confirman juntos; fallo revierte ambos. Colisión de folio se
reintenta en savepoint propio; el lock de intención no sustituye unicidad de folio.

Mismo intento recupera su resultado ANTES de consultar capturas, empresa o defaults
del día. Retorna `replayed=true` y estado vigente de la revisión si luego se editó,
no otra alta ni JSON financiero antiguo cacheado. Acceso y capacidades de lectura
se vuelven a comprobar. Misma clave/otra operación/destino/cuerpo da409. UUID previo
de revisión sin recibo API tampoco se reaprovecha. Los servicios P2 conservan su
propia huella de creación/estado; el recibo no las sustituye.

### 13.5 Preview, confirmaciones y documento

Preview retorna persistida, edit_version, state_fingerprint, resultado financiero
INTERNO, warnings y cliente (allowlist). Sin Quote, cliente.folio/revision=null;
sin escrituras/constancias/PDF. En simulación de edición la versión/huella BASE se
separan de la huella propuesta. Guardar y volver a leer antes de aceptar avisos.

GET exacto retorna originales/inputs internos, capacidades y emission_enabled,
confirmations auditables, y preview de borrador con avisos `{code,line_key|null,
requires_confirmation,required_capability,confirmed}`. Avisos informativos de stock
(`STOCK_CERO`, `STOCK_INSUFICIENTE`) llegan con `requires_confirmation=false` y
`required_capability=null`: se muestran sin botón, no bloquean emitir y confirmarlos
responde 400. `STOCK_DESCONOCIDO` sí requiere confirmación. `confirmed` solo cuenta
actor activo, permiso/acceso actuales y misma versión/huella. Las constancias
anteriores siguen auditables pero no habilitan datos cambiados o permisos revocados.
Desde P5 incluye también `tipo_cambio={valor,origen,fecha}` o null, leído de la
revisión junto con fx_context_id: mostrar evidencia guardada no crea otro contexto FX.

Confirmar: `{"edit_version":1,"state_fingerprint":"<64 hex>","warnings":[{"code":"COSTO_DESCONOCIDO","line_key":"<uuid>"}]}`.
Global usa line_key=null. No acepta actor/fecha del JSON. Emitir requiere versión y
huella guardadas; ambas se verifican BAJO los bloqueos de P3 en la operación durable.
La bandera HTTP sigue por defecto False, incluso cuando existe el motor comercial.

Emitida: detalle retorna sellos y contexto cliente archived_only, sin ejecutar el
motor. Recuperar/reintentar usa el mismo original SHA-256, no regenera ni exige
aceptar avisos históricos otra vez. PDF/correo existentes usan revisión exacta y
bytes archivados; SMTP fuera de transacción. Costos/proveedor/FX/margen/utilidad
nunca pasan al contexto cliente. El contenedor completo de preview/detalle es
EXCLUSIVAMENTE interno, no una respuesta para un portal externo.

### 13.6 Compatibilidad, errores y despliegue

- Alta sin schema_version conserva borrador v1 para transición del consumidor.
  PUT comercial v1 da409: copia explícita a v2, sin recaptura/truncamiento. Emisión
  HTTP v1 da409 aun con bandera activada; servicio interno v1 conserva históricos.
  Clonado legacy sin schema_version sigue como vía histórica, no emisión comercial.
- Errores de operaciones conservan success=false/error texto y añaden code/details:
  INVALID_INPUT400, UNAUTHENTICATED401, FORBIDDEN403, NOT_FOUND404, CONFLICT409,
  UNAVAILABLE503, INTERNAL_ERROR500. Búsqueda conserva cuerpo especializado y429;
  CSRF/método conservan respuestas del middleware/Django, no se exime la API.
- Cambiar gestión exige manage_commercial_permissions; solo cinco ajustes, booleano
  enabled estricto. No concede lectura interna, grupos, gestión o auth.change_user.
  Permiso heredado se rechaza explícitamente, sin fingir revocación.
- Pruebas amplían archivos existentes: selección estable/ajena, IVA desconocido,
  FX entre búsquedas, preview sin escrituras, corrupción de intención, reintentos,
  carreras, permisos/CSRF, confirmaciones/archivo y migración0003→0004 reversible
  SOLO en fixtures aislados. Revertir0004 en uso real perdería evidencia/recibos.
- Código requiere0003/0004 antes de usar BD de trabajo. No se aplicaron allí ni se
  dieron roles reales. No activar HTTP/mocks por desplegar esta entrega. Al cerrar
  P4 faltaba la UI consumidora (P5/P6, §§14–15); pendientes aún: validación de
  proveedores/empresa, retención/recuperación y aceptación comercial.

Validación P4: **676 pruebas Django + 6 terminal OK**, BD PostgreSQL UUID creada y
destruida, PDFs temporales y correo en memoria, red/SMTP reales bloqueados. Checks
y estado modelos/migraciones en disco consistentes. No certifica proveedores reales,
UI ni aceptación comercial; 0003/0004 no aplicadas a BD de trabajo.

## 14. P5 · Cotizador conectado al cálculo comercial

### 14.1 Alcance y presentación

`templates/cotizador.html` y `static/js/cotizador.js` consumen exclusivamente
`apiClient.commercial` de `static/js/api.js`. Se conservan tarjetas, tabla, detalles
desplegables y estilos existentes; se retiran filas/totales demo de este flujo.
`USE_MOCKS` global no se desactiva, pero ninguna página conectada inicializa sus
mocks; P6 (§15) retiró el resumen demo. La página conectada no guarda finanzas en
localStorage/sessionStorage. No hay autosave, dependencias, build o motor alterno.

- Cabecera: cliente/contacto/proyecto, fecha/vigencia y folio/revisión/versión del
  servidor. Condiciones cliente separadas de notas internas, estas solo en alta.
- Búsqueda `q` + schema_version2; filtros por código/nombre real de respuesta,
  disponibilidad desconocida distinta de cero. Cada selección conserva referencias
  context_ref/offer_ref y un line_key nuevo, sin fusionar SKU de proveedores distintos.
- Originales API de solo lectura. Base IVA UNKNOWN impide agregar y se explica; no
  se asume NET para habilitar proveedores. Otra búsqueda no reemplaza líneas/FX.
  Recapturar precios es explícito: agregar otra selección y quitar la anterior.
  El enlace del comparador solo propone un modelo para buscar, no transporta una
  captura inequívoca: se etiqueta Buscar en cotizador, precarga el término y exige
  seleccionar la oferta. No importa precio/FX ni agrega un proveedor por suposición.
- Detalle por línea: unidad original/vendida, factor divisor confirmado, cantidad
  entera, modalidad MARGIN/MANUAL, margen sobre venta o venta manual, moneda/base IVA
  y tasa incluida propia. Productos API nuevos parten de35%; conceptos manuales
  parten de venta directa sin margen automático. Campos guardados no se reconstruyen
  con35% ni con una base IVA supuesta. Costo desconocido es null, no cero.
- Ajustes: IVA venta16% inicial, descuento porcentual global antes de IVA y FX
  explícito con origen/fecha, sin tasa17.50 supuesta. Cinco capacidades controlan los
  editores; permisos se verifican siempre de nuevo en backend. Sin panel de gestión
  de otros usuarios en este incremento; la API limitada de P4 permanece disponible.
- Resumen exclusivamente interno: costos conocidos/completitud, venta/descuento/
  neto/IVA/total y utilidad solo cuando está completa. Unitarios y etiquetas de
  aproximación provienen del servidor, no de divisiones ni redondeos JavaScript.

Dinero/tasas/factores viajan como texto decimal. La validación de cantidad rechaza
`1.5`, exponentes y valores inválidos: nunca parseInt/truncamiento. El formato de
moneda separa caracteres, sin convertir importes a Number ni recalcular finanzas.
El preview no crea Quote, folio, revisión o PDF; ningún error usa totales locales.

### 14.2 Estados, guardado y conflictos

1. Los cambios invalidan el cálculo visible y aceptaciones mostradas. Preview tras
   una pausa corta; AbortController y contador de cambios descartan respuestas viejas.
   Sesión expirada, HTML CSRF, error financiero o red fallida dejan totales en —.
2. Alta lleva UUID y cuerpo estables. Una escritura incierta bloquea el formulario
   y permite resolver/repetir SOLO esa intención. Un403/CSRF posterior no descarta
   una incertidumbre anterior ni genera otro UUID. No hay reintentos automáticos.
3. Edición usa EXISTING para conservar snapshots y edit_version. Antes de repetir
   PUT se lee la revisión exacta: versión base+huella base permite el mismo PUT;
   versión base+1 con huella propuesta reconoce el guardado ya efectuado; cualquier
   otro estado exige recarga explícita. No sobrescribir cambios ajenos ni adoptar
   una versión nueva a ciegas. Si falla GET tras una escritura exitosa, conserva
   folio/identidad y exige recarga: nunca vuelve a crear la cotización.
4. Capacidades devuelve actor_id; antes de guardar, confirmar, abrir o capturar FX
   se revisa la sesión. Cambiar de asesor no reenvía el formulario del anterior.
   Esto complementa, no sustituye, autenticación, CSRF y permisos de cada operación.
5. Guardar recarga la versión exacta y su cálculo. Las advertencias se aceptan
   individualmente con versión/huella GUARDADAS y permiso vigente; preview nuevo no
   sirve para confirmar. Una edición conserva la auditoría anterior, no su vigencia.
6. Abrir por folio/número respeta acceso de objeto. V1 y emitidas son solo lectura;
   no se truncan históricos, convierten silenciosamente ni recalculan emisiones.
   Las emitidas muestran sellos/metadatos archivados; no invocan motor/renderer.

Salir/recargar avisa si hay cambios/intención pendiente. El estado no guardado vive
solo en memoria: **no se promete recuperarlo tras cerrar o recargar la pestaña**.
Conexión/permisos se pueden revisar y el login se abre en otra pestaña para conservar
la captura. Limpiar solo afecta las líneas locales, no borra historial del servidor.
No copiar datos internos a descripciones/condiciones de salida cliente.

### 14.3 Pruebas y límites operativos

- Pruebas nativas sin red/dependencias: `node --test tools/test_cotizador_comercial.cjs`.
  Si Node no está en PATH, usar su ruta local ya disponible. Cubren decimales,
  enteros, null/0, IVA incluido, referencias, reintentos, HTML/401/403/409/500 y rutas.
- Django añade contrato de evidencia FX/actor de sesión y plantilla sin demo.
  Validación de esta entrega: **678 Django + 6 terminal + 15 JavaScript correctas**;
  PostgreSQL UUID y PDFs temporales, correo en memoria, APIs/SMTP reales bloqueados.
- QA ASGI/browser aislado: búsqueda/selección, unidades/fracciones, venta manual,
  costo desconocido/0, avisos/confirmaciones obsoletas, guardado/recarga, respuesta
  perdida CREATE/PUT, CSRF tras incertidumbre, respuestas fuera de orden, conflicto,
  sesión vencida/otro asesor, capacidades, acceso ajeno y lectura emitida. Texto
  parecido a HTML se presenta como texto, no se ejecuta. FX elegido no cambia al buscar.
  Las fixtures no certifican IVA/stock o contratos de SYSCOM/Exel reales.

Al cerrar P5, resumen/emitir/nueva revisión/PDF/correo estaban deshabilitados y el
resumen anterior marcado Demo. P6 (§15) sustituye esa pantalla y conecta acciones
por revisión exacta; no habilita el flag de emisión en el entorno habitual.
No hay migración adicional ni cambio del formato persistido. 0003/0004 continúan
pendientes en BD de trabajo y la emisión HTTP sigue OFF. Tampoco se asignaron roles
reales, alteró .env o validaron datos oficiales/retención/recuperación. No arrancar
esta versión con la BD de trabajo sin autorización para preparar ese entorno.

### 14.4 M2 · Condiciones predeterminadas

Textos que el cotizador **precarga** en "Condiciones para el cliente" de cada cotización
nueva. Son solo un valor inicial: el asesor los edita o borra antes de guardar.

**Cómo se usan (administrador):**
- Admin de Django → **Quotes → Condiciones predeterminadas**: agregar, editar el
  **orden** y marcar o desmarcar **activa** (también desde la lista).
- Una condición por registro: sin saltos de línea, hasta 6000 caracteres y máximo
  **50 activas** (el mismo límite que acepta el cotizador). Los espacios al inicio y
  al final se quitan al guardar.
- Desactivar en lugar de borrar conserva el texto para volver a usarlo.
- Hoy solo entra al admin un usuario `is_staff` con permisos del modelo (p. ej. un
  superusuario); el grupo «Administrador CCONOR» no tiene acceso al admin.

**Cómo funciona:**
- Modelo `DefaultCommercialCondition` (`text`, `order`, `active`; orden por `order` y
  luego por alta), migración `quotes.0006` (solo crea la tabla).
- La vista `cotizador_comercial` (`/cotizador/`, con sesión) entrega las activas en
  orden con `json_script` (`#defaultConditions`); el texto se escapa y nunca se
  ejecuta como HTML.
- `cotizador.js` las escribe en el cuadro al **abrir la página** y al pulsar **«Nueva
  cotización»**. Abrir una revisión existente o preparar una copia desde un histórico
  v1 muestra las condiciones de esa cotización, no las predeterminadas.
- Precargar no marca la cotización como modificada.
- El API no cambia: `conditions` sigue siendo una lista explícita y obligatoria que
  se guarda tal cual en cada revisión (§12). Cambiar o desactivar una condición
  predeterminada **no altera** cotizaciones guardadas ni emitidas.

Pruebas: `CondicionesPredeterminadasTests` (`test_vistas_cotizacion.py`) y casos M2 de
`tools/test_cotizador_comercial.cjs`. Estado y textos oficiales: [docs/30](30-puesta-en-marcha.md) M2 y B1.

### 14.5 Ajustes visuales del cotizador (2026-10-19)

Solo presentación (plantilla, `cotizador.js`, `custom.css`): el cálculo, el API y los
datos guardados no cambian.

- **Pasos numerados:** 1 Datos del cliente → 2 Tipo de cambio del día → 3 Productos y
  conceptos → 4 Revisar y guardar. El tipo de cambio va antes de la búsqueda porque las
  ofertas en dólares lo necesitan; IVA y descuento quedan en «Ajustes del total».
- **Avisos:** al guardar se revisan todos los pendientes a la vez. Uno solo se muestra
  como aviso emergente persistente con «Ir al campo/Ir a la partida»; varios abren una
  ventana con la lista y un botón por pendiente. El campo o la partida se marcan en rojo
  hasta corregirlos. Los errores del servidor usan el mismo aviso; los de búsqueda por
  falta de tipo de cambio ofrecen «Ir al tipo de cambio». El cálculo automático mientras
  se captura no lanza avisos: el resumen indica qué dato falta. Solo lectura e
  histórico v1 se informan con un mensaje azul, no como error.
- **Detalle de partida:** tres columnas (Del proveedor / Concepto y costo · ¿Cómo lo
  vendes? · Precio al cliente) con opciones en botones. «Igual que el proveedor» usa
  una sola unidad (factor 1); «Por partes» pide la unidad del proveedor, la tuya y
  cuántas trae («1 Bobina = 100 Metro»). La casilla «Revisé…» sigue siendo obligatoria.
  El resumen por partida (costo y precio por unidad, importe, utilidad) viene del preview
  del servidor; los redondeos de pantalla se marcan con ≈.
- **IVA en porcentaje:** la pantalla muestra «16»; el navegador envía la fracción exacta
  `0.1600` (conversión de texto con BigInt, sin float). Igual para el IVA incluido de
  costos o precios manuales.

- **Encabezado:** título, folio · revisión y una etiqueta de estado con color (gris nueva,
  ámbar cambios sin guardar, verde guardado, azul solo lectura, rojo sesión vencida o
  conflicto) y «Nueva cotización». Al bajar, el menú del sitio con el logo deja de ser fijo
  (solo en esta página) y aparece una barra fija con folio, estado, total y los botones de
  nueva cotización y guardar (delegan en los principales). Ya no hay botones «Revisar
  conexión» ni «Iniciar sesión»: la sesión y los permisos se revisan solos al volver a la
  pestaña (como mucho cada 15 s) y el aviso de sesión vencida ofrece iniciar sesión.
- **Paso 4:** el recuadro «Avisos de esta cotización (N)» solo aparece cuando el cálculo
  trae avisos. Botones de 44 px alineados a la derecha (diseño 6D): «Limpiar partidas» en
  rojo (borra todas las partidas), «Ver resumen» y «Guardar borrador». Se quitó «Abrir
  revisión conocida»: otra cotización se abre desde «Mis cotizaciones» (enlace al pie del
  paso 4).
- **Colores de botones (todo el sitio, opción 4C):** principales y de contorno en azul
  marino `#111638`; opción elegida, números de paso y casillas en verde azulado `#00796B`
  (`custom.css`, `home.css`). Rojo, gris y amarillo se conservan para quitar, neutro y estado.

Pruebas: casos de conversión, redondeo, unidades y campo de cada error en
`tools/test_cotizador_comercial.cjs`; recorrido en navegador aislado.

### 14.6 Inicio como panel de cotizaciones (diseño 7D)

- `/` (`templates/inicio.html`) muestra el panel y exige sesión. La portada pública (hero, beneficios y
  categorías) se quitó el 2026-10-20 junto con sus estilos de `home.css` (ver §14.7).
- **Contenido:** saludo («Hola, nombre»), «Buscar productos» (comparador) y «+ Nueva cotización»;
  tres tarjetas (borradores por terminar, emitidas, emitidas este mes) y dos paneles,
  **Borradores** y **Emitidas**, con las 3 más recientes de cada uno.
- **Datos:** `static/js/inicio.js` pide dos veces el listado de «Mis cotizaciones»
  (`GET /api/v1/quotes/?status=DRAFT|ISSUED&page=1`, una fila por folio, última revisión). No
  hay endpoint nuevo ni cambio de datos guardados. Los conteos salen de `pagination.count`;
  «emitidas este mes» cuenta las filas de la primera página cuya `issue_date` es del mes en
  curso (si todas lo son y hay más páginas, muestra «N+»). Los importes se muestran como texto
  del servidor, sin convertir a número.
- **Acciones:** borrador → `/cotizador/?folio=…&revision=…` («Continuar editando»); emitida →
  `/resumen/?folio=…&revision=…` («Ver resumen») y `/cotizaciones/<folio>/pdf/?revision=…`
  («PDF»). Un histórico v1 solo ofrece «Ver resumen» (su PDF puede traer costos y se abre desde
  el resumen, con su advertencia). **«Nueva revisión» no está en el panel:** esa operación,
  con su confirmación y manejo de resultado incierto, se hace desde el resumen.
- **Sin permisos o con error:** el aviso en ámbar muestra el mensaje del servidor y las listas
  quedan con un texto de «no se pudieron cargar»; nunca se inventan datos.
- **Caché:** `home.css?v=4`, `inicio.js?v=1`.

Pruebas: `test_p8`/`test_p9` en `test_vistas_cotizacion.py` (panel solo con sesión) y cuatro casos
de `inicio.js` en `tools/test_cotizador_comercial.cjs` (formatos, enlaces, llenado y error).

### 14.7 Encabezado y pie del sitio (plataforma interna)

- **Sin barra superior** en todas las páginas (`templates/base.html`): se quitaron teléfono,
  correo, ubicación y redes sociales, porque la plataforma es de uso interno de CCONOR. El menú
  con el logo queda arriba, sin marco. También se quitaron sus estilos (`.cconor-topbar`).
- **Pie de una línea (diseño 10B):** «CCONOR · © 2026 Soluciones Tecnológicas», distribuidores
  (SYSCOM, Exel del Norte) y «Portal de Cotización Multi-Proveedor». Sin «Navegación» ni
  «Contacto». En celular se apila en tres líneas centradas.
- El aviso emergente del cotizador sube 36 px (`top: 124px`), la altura de la barra quitada, para
  conservar la misma separación bajo el menú. Caché: `custom.css?v=9`.
- Los datos de contacto de la empresa siguen en el PDF y en el correo (`pdf.py`, `views.py`).

Prueba: `test_p10_sin_barra_superior_y_pie_de_una_linea` (Inicio, cotizador, resumen y comparador).

**Sesión (diseños 13B y 13C, 2026-10-20).** Sin sesión no se muestra nada del sistema: `base.html`
dibuja el menú superior y el pie solo con `user.is_authenticated`, `/` también usa `login_required` y
cualquier página lleva a `/login/?next=…`, una tarjeta centrada con el logo (sin menú ni pie). En el
menú, «SALIR (usuario)» se cambió por un desplegable de Bootstrap con las iniciales en un círculo verde
azulado y el primer nombre (o el usuario si no hay nombre); dentro: nombre completo, «Sesión iniciada
como …» y «Cerrar sesión» (POST con CSRF). Se cierra al pulsar fuera o con Esc. Los mensajes de Django
solo crean su contenedor cuando hay mensajes. Caché: `custom.css?v=10`, `home.css?v=5`. Pruebas:
`test_p2`, `test_p7`, `test_p8` y `test_p9` en `test_vistas_cotizacion.py`.

### 14.8 Comparador: botón «Buscar» y tipo de cambio (diseño 11B)

- Fila de búsqueda: búsqueda libre, modelo exacto, categoría y «Buscar», con la misma altura que
  los campos (`.comparador-buscar`) y la lupa junto al texto.
- El tipo de cambio va en su propia línea como frase: «Tipo de cambio para precios en dólares:
  1 USD = $ [Ej. 18.50] MXN» (el ejemplo en gris e itálica; lo escrito, en negritas). Ya aplicado
  se muestra como etiqueta verde «✓ 1 USD = $X MXN», la hora de captura (UTC) y «Cambiar», que abre
  el campo con el valor anterior. Sustituye la línea gris que aparecía sobre los resultados.
- Sin cambio de funcionamiento: el valor se sigue enviando con cada búsqueda (GET) y no se recuerda
  entre visitas. Estilos dentro de `comparador.html`.
- **Avisos como ventana emergente** (igual que el cotizador): el error («No se pudo buscar») y
  «Algunos proveedores no respondieron» (en ámbar) salen arriba a la derecha bajo el menú, con X
  para cerrar (Bootstrap). Si el error es del tipo de cambio, «Escribir tipo de cambio» cierra el
  aviso y pone el cursor en el campo (`data-enfocar` en `static/js/main.js?v=3`).
- La flecha del título pasa de naranja a verde azulado (`--cconor-eleccion`, color de lo elegido en 4C).
- **Tipo de cambio recordado en la sesión** (2026-10-20): el último valor válido del comparador se
  guarda en la sesión (`comparador_tipo_cambio`: valor y hora de captura con zona) y se reutiliza al volver
  o al buscar sin escribirlo, con su hora original. Otro valor lo reemplaza; uno inválido no; enviarlo
  vacío (desde «Cambiar») lo olvida; cerrar sesión también. Se avisa con «se conserva durante tu sesión»
  y, si es de otro día, «Capturado otro día: confirma que siga vigente». Solo el comparador: la búsqueda
  del cotizador no lee ni escribe esta clave (tiene su propio contexto de tipo de cambio con permisos).
- **Sugerencia del tipo de cambio de SYSCOM (14A, 2026-10-20):** si no hay uno aplicado y SYSCOM está
  activo, junto al campo aparece «SYSCOM hoy: $X (contado) · Usar». **Nunca se aplica solo.** «Usar» es un
  botón del mismo formulario (`usar_tipo_cambio=syscom`): el servidor toma el valor de SYSCOM (no el del
  formulario), busca con él y lo guarda con origen `syscom`, que la etiqueta muestra como «SYSCOM · fecha».
  Lo escrito a mano manda sobre «Usar». El valor se guarda en caché 30 min (1 min tras un fallo); si
  SYSCOM no responde: «No se pudo consultar el de SYSCOM; escríbelo a mano.» Sin SYSCOM activo no se
  consulta ni se muestra nada. Campo: `SYSCOM_TIPO_CAMBIO_CAMPO` (docs/17 §3.3).
- **Aviso si SYSCOM cambia (2026-10-20):** si el valor aplicado vino de SYSCOM y SYSCOM ya da otro,
  junto a la etiqueta aparece «SYSCOM ahora: $X · Actualizar». **Nunca se actualiza solo**: «Actualizar»
  (`usar_tipo_cambio=actualizar`) toma el valor nuevo de SYSCOM, busca con él y lo guarda con su hora. Un
  valor capturado a mano no recibe este aviso.
- **Un solo tipo de cambio por sesión (15A y 16A, 2026-10-20).** La misma clave `comparador_tipo_cambio`
  sirve al comparador, al menú y al cotizador. `GET/PUT /api/v1/fx/session/` (`api.tipo_cambio_sesion`,
  solo con sesión) devuelve `{session: {value, date, source} | null, syscom: {value, label} | null,
  syscom_failed}`. `PUT {"value": "18.70"}` lo guarda como manual (mismos límites que el cotizador:
  18 dígitos, 8 decimales, > 0); `PUT {"use_syscom": true}` toma el de SYSCOM del servidor (503 si no
  responde, sin cambiar nada). El mismo valor escrito otra vez (18.54 = 18.54000000) conserva su hora y
  origen. No toca ninguna cotización.
  - **15A · chip en el menú** (`base.html`, `static/js/main.js`): verde «✓ USD $X · SYSCOM/manual» o
    gris «USD sin capturar». Al pulsarlo: valor, origen y hora, «Capturado otro día», «SYSCOM hoy · Usar»
    (o «SYSCOM ahora · Actualizar» si el de la sesión vino de SYSCOM y cambió) y «Otro valor · Aplicar».
    En el comparador, cambiarlo vuelve a buscar con el valor nuevo.
  - **16A · cotizador, paso 2**: atajos «Tu sesión: $X (origen, hora) · Usar» y «SYSCOM hoy: $X · Usar»;
    con un tipo de cambio ya aplicado, «Tu sesión» solo si es distinto, y «SYSCOM ahora · Actualizar» si
    se capturó otro día o el de la sesión vino de SYSCOM. «Usar» llena el campo y pulsa «Aplicar» (mismo
    permiso de tipo de cambio de siempre); al aplicarse también queda como el de la sesión. **Nada se
    aplica solo.**
  - La cotización sigue guardando el origen `manual` (decisión del usuario): el cliente nunca ve el tipo
    de cambio; el PDF comercial v2 no lo muestra (solo los PDF históricos v1 lo traían).

## 15. P6 · Mis cotizaciones, resumen y documentos exactos

> **Rediseño 12A (2026-10-20, rama `feature/ui-interfaz-general`).** La pantalla pasó a lista y
> detalle lado a lado: a la izquierda buscador, filtros (Todas, Borradores, Emitidas) y la lista
> (folio · revisión, estado, cliente, total de las emitidas, fecha); a la derecha la cotización
> elegida con total, pestañas de revisiones, **solo los botones de su estado** (borrador: Editar en
> el cotizador, Emitir, PDF borrador, Imprimir; emitida: Enviar por correo, Descargar PDF, Imprimir,
> Nueva revisión; histórico v1: PDF con advertencia y «Preparar revisión comercial») y la vista
> previa, que es siempre **el PDF real** (borrador con su marca u original archivado), idéntico al que
> se descarga; se quitó la vista HTML que lo imitaba. El correo se abre en una ventana (diseño 12D). Se
> quitaron la captura manual de folio y revisión, «Revisar sesión» e «Iniciar sesión en otra
> pestaña»: la sesión se revisa al volver a la pestaña (como mucho cada 15 s) y, si venció, el aviso
> emergente ofrece «Iniciar sesión». Se conservan intactas las reglas de esta sección (identidad
> exacta, emisión sin avisos pendientes, envío sin reintento automático, operación incierta, enlace
> `?folio=&revision=`). Archivos: `templates/resumen.html`, `static/js/cotizacion_actions.js?v=7`.

### 15.1 Alcance y acceso

Se reemplaza el resumen estático: ningún folio, cliente, importe, cuenta bancaria o
condición ficticia se usa como fallback. El diseño conserva las tarjetas/estilos
existentes. `cotizacion_actions.js` controla listado, revisión, PDF y correo;
`apiClient.commercial` se mantiene independiente de los mocks globales. El resumen
conectado tampoco inicializa almacenamiento mock ni usa storage financiero.

Guardar permanece en el cotizador. **Ver resumen** solo se habilita para una revisión
guardada sin cambios pendientes/intenciones inciertas; abre folio+número explícitos.
**Editar borrador / confirmar avisos** vuelve a esos mismos identificadores. Una
emitida no se edita: **Nueva revisión** usa el servicio/recibo P4, sin sobrescribirla.

Todas las lecturas financieras requieren usuario interno activo y permiso vigente.
El listado es SIEMPRE de `advisor_id=actor.pk`, incluso para staff/superusuario. El
acceso exacto conserva propietario/staff más capacidad financiera, sin ampliarlo
por pertenecer al rol Administrador CCONOR. No hay gestión general de usuarios,
borrado, portal público ni estados comerciales nuevos como aprobada/enviada.

### 15.2 Lecturas y listado propio

| Método/ruta | Resultado |
| --- | --- |
| GET `/api/v1/quotes/?q=&status=&page=` | Última revisión de cada folio propio con revisiones capturadas; actor_id, data y pagination |
| GET `/api/v1/quotes/<folio>/revisiones/<n>/resumen/` | Identidad/estado/versión/huella, quote_status separado, metadatos de revisiones, avisos y contexto cliente allowlist o null para v1 |
| POST `.../revisiones/<n>/nueva/preview/` | Simulación de transición v1 explícita, sin crear Quote, revisión, constancias o archivo |

Listado:20 filas/página, orden por fecha de creación de última revisión e ID
descendentes. `q` busca folio o cliente de la cabecera de esa revisión (no el nombre
antiguo en Quote), máximo200 caracteres. `status` vacío/DRAFT/ISSUED filtra estado
de la ÚLTIMA revisión, no quote_status ni estado de envío. Página positiva acotada;
filtros desconocidos, duplicados o página fuera de rango dan400. Un fallo no se
convierte en listado vacío ni carga datos simulados. Quote sin revisión capturada
queda fuera de este listado; su consulta histórica existente no se elimina.

Los importes del listado son solo sellos: un borrador devuelve total=null, no0 ni
el total de una emisión anterior. No ejecuta el motor para cada fila ni usa
QuoteItem/propiedades financieras legacy. La paginación y la selección no conceden
acceso a otra cotización. Datos de lista/summary llevan `Cache-Control: private, no-store`.

Resumen v2 DRAFT obtiene el cálculo del servidor y su contexto cliente, sin
serializar costos, porcentaje de margen, FX, proveedor, utilidad o notas internas.
Los avisos fuera del documento indican si la aceptación guardada sigue vigente.
Resumen ISSUED usa metadatos archivados y muestra el **PDF original verificado**:
no reconstruye unitarios ni vuelve a ejecutar motor/renderer. Si falta el archivo,
se informa el fallo; no se sustituye por un HTML/PDF recalculado o un demo.

### 15.3 Descarga e impresión

La UI utiliza la ruta existente con identidad completa:

```text
/cotizaciones/<folio>/pdf/?exact=1&revision=N&edit_version=V&expected_status=DRAFT&state_fingerprint=<64hex>
```

La comparación de número/versión/estado/huella se hace dentro del bloqueo de lectura
en `obtener_documento`, antes de materializar el contexto. Si editaron o emitieron
después de cargar el resumen, devuelve409 y exige recarga explícita: nunca elige
otra revisión. Una lectura exacta v2 exige huella; v1 no tiene huella comercial.
Parámetros de identidad duplicados o incompletos dan400.

Servicios de documento agregan argumentos opcionales `expected_version`,
`state_fingerprint`, `expected_status` y `customer_only`, sin cambio de esquema.
La firma anterior sigue para consumidores internos/históricos; sin número conserva
su preferencia histórica por última emitida, pero el flujo P6 NO usa ese modo.

Un borrador produce PDF marcado BORRADOR, sin archivo de emisión. Una emitida lee
bytes privados y verifica SHA-256. **Abrir PDF / imprimir** abre esos bytes en el
visor del navegador, no llama a window.print sobre la pantalla interna. Descargar
valida respuesta PDF; HTML de login/CSRF o JSON de error nunca se guarda como PDF.
El blob vive temporalmente en memoria, no se publica ni persiste como dato financiero.

### 15.4 Emisión, copia y resultados inciertos

Emisión explícita con confirmación del usuario; versión/huella guardadas y avisos
aceptados, revalidados por M8. `emission_enabled=false` deshabilita la acción, sin
cambiar settings ni convertir guardar en emitir. No se envía correo automáticamente.
Tras una respuesta perdida, el botón de resolver lee la misma revisión: mismo
estado/versión/huella emitidos reconoce éxito sin otro POST; borrador intacto permite
el mismo intento; cualquier diferencia exige recarga. No adoptar una versión ajena.

Copia v2 mantiene UUID/cuerpo al recuperar su recibo. Un403/CSRF posterior a una
incertidumbre no descarta la intención anterior. Si la escritura tuvo éxito y
falló la lectura siguiente, conserva folio/número de destino y pide recargar esa
revisión, no crear otra. Navegación y acciones se bloquean mientras se resuelve.

Sesión/capacidades se consultan antes de mutar. Si cambió de asesor, no se reenvían
los datos del anterior. Sesión fallida oculta documento/listado y permite iniciar
sesión en otra pestaña; backend mantiene comprobaciones frescas de cada petición.
Respuestas antiguas de búsqueda/lectura/PDF se descartan al cambiar de selección.

### 15.5 Correo y documento histórico

POST existente `/api/cotizaciones/<folio>/enviar-correo/` exige ahora `revision`
explícita, acceso financiero interno y **revisión v2 emitida**. V1/sin revisión
capturada no se envían al cliente. `email`, `subject`, `message` siguen editables;
no cambian la captura del cliente. Límites254/255/6000 caracteres, tipos estrictos,
campos adicionales/JSON con claves duplicadas rechazados. CSRF se conserva.

La UI manda además `exact=true`, edit_version, expected_status=ISSUED,
state_fingerprint y attach_pdf=true. Este modo siempre adjunta el original exacto;
`attach_pdf` ausente equivale a true y `attach_pdf=false` se rechaza (400). Confirmación identifica destinatario,
folio y revisión. Un retorno1 del backend SMTP significa aceptación, no recepción
garantizada. La acción ocurre fuera de transacciones de BD; no hay auditoría ni
idempotencia SMTP nueva. Un corte/500 puede ocurrir después de enviar: se muestra
resultado desconocido y no se reintenta. Habilitar otro envío requiere que el usuario
confirme haber verificado recepción/registros; no se simula verificación automática.

V1/v2 son FORMATOS, no números de revisión. El nuevo resumen v1 no publica contexto
cliente: etiqueta histórico interno, deja consultar su original autorizado y bloquea
correo/emisión comercial. **Preparar revisión comercial v2** abre el cotizador con
origen explícito, SIN copiar precios, moneda, factor, cantidades o márgenes por
suposición. Se agregan/validan capturas nuevas y se calcula su preview de servidor.

La transición manda schema_version2, source_edit_version, items, tax_rate,
discount_percent y fx_context_id; guardar añade request_id estable y reutiliza
POST `.../nueva/`. Preview valida versión origen; crear comprueba esa versión antes
de escribir (replays resuelven recibo primero). Cabecera de origen preservada al
crear el borrador, fecha nueva del servidor; cliente/condiciones se revisan y pueden
editarse DESPUÉS de guardar, antes de emitir. No se heredan aceptaciones. El original
v1 y sus revisiones anteriores permanecen intactos, sin regeneración ni conversión
con defaults ciegos. No es recotización automática M9.

### 15.6 Verificación y límites

Pruebas amplían archivos existentes: listado propio incluso staff, paginación,
filtros, cliente actualizado, sesión/permisos, summary allowlist sin escrituras,
lectura emitida sin motor/render, PDF con versión/estado/huella obsoletos, correo
original/adjunto/destinatario, fallos de aceptación SMTP, v1 interno y transición
explícita. Node nativo amplía `tools/test_cotizador_comercial.cjs`, sin dependencias.
Validación final: **694 Django +6 terminal +22 JavaScript correctas**, checks Django,
modelos/migraciones en disco, sintaxis y enlaces/diff OK. PostgreSQL UUID/PDF
temporales creados/eliminados; correo en memoria, APIs/SMTP reales bloqueados,
.env sin cambios. Se conservan pruebas de concurrencia y originales v1/v2.

Recorrido ASGI/browser sintético: listado23 folios con20/3 filas y búsqueda,
resumen exacto, edición/guardado sin navegación automática, Ver resumen, emisión
perdida reconocida sin segundo POST, copia perdida con mismo UUID/cuerpo, lectura
posterior fallida conservando nuevo número, PDF obsoleto bloqueado, PDF borrador en
visor nativo, correo incierto sin reenvío, transición v1→v2 desde entradas explícitas,
confirmaciones, texto similar a HTML sin ejecución y sesión vencida/otro asesor.
Pantalla500px sin overflow global (tablas con scroll local); visor original y
borrador revisados. Solo pestañas/BD/carpeta/registros propios P6 se cerraron y
eliminaron; no se tocaron recursos QA antiguos ni pestañas previas del usuario.

Sin modelos/migraciones nuevos, dependencias o build. 0003/0004 no aplicadas a BD
habitual; la emisión HTTP habitual sigue OFF. QA privado usa BD/PDF/usuarios/ofertas
sintéticos y correo en memoria, con APIs/SMTP reales bloqueados; activar emisión
solo en ese perfil no habilita operación real. Bases IVA reales siguen UNKNOWN.
La UI no certifica datos oficiales, reglas de proveedores ni backups/recuperación;
eso continúa pendiente de P7 y autorización operativa. Colores/rediseño y gestor
de permisos de terceros no forman parte de esta entrega.
