# 25 · Integración M8 para M7, M9, M10, M11 y M13

Diseño/límites: [24 · SDD](24-sdd-m8-snapshot-pdf.md). M6:
[23](23-m6-comparacion-y-moneda.md). [DER](DER.md).

## 1. Entrada normalizada y proveedores futuros

Importar de `apps.quotes.snapshots`. M8 no interpreta payloads ni selecciona
APIs/proveedores. Un adaptador futuro que entregue `ProviderOffer` con precios
base comparables en USD/MXN se integra sin modificar servicios M8.

`PartidaCapturada` es frozen: `name`, `source_unit_price`, `source_currency`,
`quantity` obligatorios; `code`, `description`, `provider_code`, `provider_name`,
`external_id`, `brand`, `model`, `mpn`, `stock`, `es_mejor_precio` opcionales.
No recibe `raw`, producto ORM ni precio comparable redondeado.

```python
linea = PartidaCapturada.desde_oferta(oferta, Decimal("2"), nombre_proveedor="Nombre verificado")
linea = PartidaCapturada.desde_comparada(comparada, Decimal("2"), nombre_proveedor="Nombre verificado")
```

El segundo copia oferta original y marca M6, no `precio_comparable` como origen.
La descripción se aporta con `dataclasses.replace(linea, description="...")`:
M2 no tiene ese campo. El consumidor resuelve nombre comercial, no hardcodearlo.

Stock `0` es sin existencias y `None` desconocido. Mejor precio es **por proveedor**,
no confirma stock/equivalencia. M7 conserva avisos de errores/empates M6 y revisión
manual M5; no guarda todas las alternativas como partidas de una compra.
No usar ejemplos de Exel como verificación de IVA/precios/stock reales.

## 2. Servicios soportados

Requieren actor autenticado/activo, propietario de Quote o staff. Devuelven
`QuoteRevision`, excepto documento: `(contexto_materializado, bytes_pdf, nombre_descarga)`.

### Crear borrador

```python
crear_borrador(
    quote_id, partidas, *, actor, company, conditions,
    tipo_cambio=None, client=None, tax_rate=None, issue_date=None,
    valid_days=None, request_id=None,
)
```

- Quote debe existir: M8 no crea su cabecera legacy.
- Partidas: iterable materializado de `PartidaCapturada`, 1–500 líneas.
- Empresa: dict `name`, `rfc`, `tagline`, `phone`, `email`; `name` obligatorio.
  El servicio aporta `advisor` desde el propietario, no desde texto de la UI.
- Condiciones: lista/tupla explícita; puede estar vacía, sin términos inventados.
- Cliente: dict `business_name`, `rfc`, `contact`, `email`, `phone`, `project`.
  Omitido: captura nombre/RFC/email de Quote, restantes vacíos.
- Tasa IVA/fecha/vigencia omitidas: valores de Quote. La fecha comercial no se
  actualiza automáticamente al emitir; confirmarla antes de emisión tardía.
- FX: `TipoCambio` M6 o None, obligatorio con USD.
- `request_id`: UUID generado/guardado por M7 antes de enviar. Omitirlo genera
  otro y un reintento **puede crear un borrador duplicado**.

Repetir clave/solicitud devuelve revisión original aunque después se editara o
emitiera. Misma clave con otra solicitud: `RevisionConflict`. La huella incluye
cabecera/defaults capturados; si cambian, la solicitud no es idéntica. Mandar
defaults explícitos para reintentar; no sustituir UUID tras una respuesta perdida.

### Edición completa

```python
guardar_borrador(
    revision_id, partidas, *, actor, expected_version,
    company, conditions, client, tipo_cambio, tax_rate, issue_date, valid_days,
)
```

Reemplazo completo, no PATCH: enviar también datos que se conservan.
`expected_version=revision.edit_version`. Incrementa versión y reemplaza partidas
en una transacción. Una sesión obsoleta requiere recarga/decisión del usuario;
no reintentar a ciegas con versión nueva. USD exige FX en el conjunto reemplazado.

### Emisión

```python
emitir_revision(revision_id, *, actor, expected_version)
```

Confirmar fecha/vigencia, cliente, precios, condiciones y FX. No consulta APIs.
Emite la versión exacta; si ya está emitida, verifica/reutiliza original sin
regenerarlo. No permite editar la emisión.

**No envolver en atomic, ATOMIC_REQUESTS ni una transacción de vista.** La atomic
durable pertenece al servicio. SMTP después del commit. Un error de commit no
garantiza DRAFT: verificar/reintentar misma revisión/versión; no crear sustituta
automáticamente. El archivo se conserva cuando el commit no está confirmado.

### Nueva revisión

```python
crear_revision(revision_id, *, actor, request_id=None)
```

Origen ISSUED. Copia datos comerciales/partidas/FX/IVA; captura fecha de hoy y
asesor actual. Crea DRAFT, número nuevo, `based_on`, `edit_version=1`, sin PDF ni
totales sellados. No implica nueva consulta API. UUID permite reintentos incluso
otro día; una clave de otra operación/origen genera conflicto. Después editar con
`guardar_borrador` y emitir por el servicio normal.

### Documento

```python
obtener_documento(folio, *, actor, number=None, allow_draft=True)
```

- Número explícito: esa revisión, nunca otra por fallback.
- Sin número: última ISSUED; si no hay emisiones, último DRAFT.
- Un nuevo borrador no oculta original anterior: pasar número para verlo.
- DRAFT: PDF marcado, no escribe archivo; `allow_draft=False` da conflicto.
- ISSUED: lee original/verifica SHA-256, sin ejecutar renderer.
- Quote sin revisiones: representación histórica etiquetada, sin inferir moneda.

`contexto_revision` y `leer_pdf_emitido` son helpers internos **sin autorización**.
No exponerlos en endpoints; usar `obtener_documento`. No llamar `get_quote_context`
para revisiones M8 ni escribir modelos directamente.

## 3. Ejemplo M6 → M8

Para Django shell, con `quote_id`/`actor` reales ya resueltos y datos comerciales
validados. Proveedor/datos siguientes son **sintéticos**:

```python
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from apps.providers.contracts import ProviderOffer, ProviderResult
from apps.quotes.comparison import comparar, tipo_cambio_manual
from apps.quotes.snapshots import (
    PartidaCapturada, crear_borrador, emitir_revision, obtener_documento,
)

tipo = tipo_cambio_manual("17.12345678", fecha=datetime.now(timezone.utc))
oferta = ProviderOffer("nuevo", "sku-1", "Equipo sintético", Decimal("382.533200"), "USD", stock=0)
resultado = comparar([ProviderResult("nuevo", offers=(oferta,))], tipo_cambio=tipo)
seleccionada = resultado.por_proveedor["nuevo"][0]  # selección explícita
linea = PartidaCapturada.desde_comparada(seleccionada, Decimal("2"), nombre_proveedor="Proveedor sintético")
clave_guardado = uuid4()  # conservar antes de enviar/reintentar
borrador = crear_borrador(
    quote_id, [linea], actor=actor,
    company={"name": "Empresa sintética"}, conditions=["Ejemplo; no usar comercialmente."],
    tipo_cambio=resultado.tipo_cambio, request_id=clave_guardado,
)
# Tras confirmación del usuario, sin atomic externa:
emitida = emitir_revision(borrador.pk, actor=actor, expected_version=borrador.edit_version)
contexto, pdf_original, filename = obtener_documento(
    emitida.header["folio"], actor=actor, number=emitida.number,
)
```

Persistir el mismo `resultado.tipo_cambio`, no otra tasa tomada después.
Serializar Decimal como texto en HTTP, validar sin float antes de crear partida.
No pasar por precio de dos decimales de Product/QuoteItem ni inferir moneda de
legacy: recapturar exige evidencia explícita y operación consciente.

## 4. ASGI / M7a

Wrappers: `acrear_borrador`, `aguardar_borrador`, `acrear_revision`,
`aemitir_revision`, `aobtener_documento`, mismos argumentos. Usan
`sync_to_async(..., thread_sensitive=True)` sobre la operación completa.

```python
borrador = await acrear_borrador(quote_id, lineas, actor=actor,
                                company=empresa, conditions=condiciones,
                                tipo_cambio=tipo, request_id=clave)
version = borrador.edit_version  # ya materializado
# No consultar borrador.snapshots.all() o borrador.quote en el event loop.
contexto, pdf, filename = await aobtener_documento(folio, actor=actor, number=borrador.number)
```

Resolver actor/ORM por vías async apropiadas (`request.auser()` en nuevas vistas
async). Consultas API siguen con `asyncio.gather`; no paralelizar emisión/edición
de la misma revisión. Endpoints heredados descarga/correo siguen sync bajo ASGI;
no duplicar renderer ni transacción en la vista.

## 5. Rutas y errores

| Ruta | Contrato |
| --- | --- |
| GET `/cotizaciones/<folio>/pdf/?revision=N` | Propietario/staff, original ISSUED o borrador marcado |
| POST `/api/cotizaciones/<folio>/enviar-correo/` | Propietario/staff; email, subject?, message?, attach_pdf?, revision? |
| GET `/demo/cotizaciones/<folio>/pdf/` | Demo explícito solo DEBUG, sin consultar BD |

Correo no emite ni regenera original. `attach_pdf=false` no evita autorización ni
permite borradores: verifica original incluso sin adjunto. Legacy puede enviarse
etiquetado, sin afirmar MXN/original archivado. No hay auditoría/idempotencia SMTP:
dos POST pueden mandar dos correos. Mantiene CSRF Django; descarga no se cachea
(`private, no-store`). La plantilla identifica revisión y total real.

Sin sesión Django, `login_required` redirige (302). M7 debe conectar login real y
configurar LOGIN_URL; mocks/localStorage no equivalen a autenticación de servidor.
Los endpoints de creación/emisión todavía no existen: M7 integra estos servicios.

| Situación | Servicio | HTTP actual |
| --- | --- | --- |
| Tipo incorrecto | TypeError | M7 debe traducirlo al validar frontera |
| Precisión/rango/estructura inválidos | ValidationError | 400 |
| Sin acceso | PermissionDenied | 403; sin login, 302 en vistas |
| Quote/revisión ausente | DoesNotExist | 404 |
| Versión obsoleta/editar emisión/enviar borrador | RevisionConflict | 409 |
| Original ausente/corrupto/escritura corrupta | ArchivedPDFError | 503 |
| Error operacional DB/storage/renderer | Excepción | 500 genérico, sin detalle sensible |

No convertir todas las excepciones en 404 ni devolver demo/datos parciales/PDF
regenerado ante fallo de original.

## 6. Responsabilidades del equipo

### M7a / M7b: flujo e interfaz

- Selección explícita, sesión/CSRF, permisos y servicios, sin escrituras directas.
- Mostrar número/estado/versión, precios de origen/convertidos, FX/origen/fecha,
  stock cero/desconocido, errores/empates y revisión manual, sin ocultar precisión.
- Retener UUID en reintentos y versión optimista en edición; 409 requiere recarga.
- Acciones distintas: guardar, emitir, descargar original, nueva revisión.
- Emisión sin campos editables. Totales M8, nunca Quote.total/JavaScript divergente.
- Validar empresa, logo, condiciones, fecha/vigencia y remitente; no usar datos demo.

### M9: re-cotización

La excepción antigua de refrescar durante dos horas **no autoriza sobrescribir
emisiones**. Aplicarla solo a borradores o nuevas revisiones:

1. Si origen emitido, crear revisión idempotente y conservar PDF previo.
2. Consultar APIs fuera de transacción M8; reconciliar IDs, matching, selección,
   precio, moneda, stock y disponibilidad con usuario.
3. Partidas nuevas con originales y FX explícito; reemplazar borrador usando versión.
4. Emitir por M8, manteniendo ambos originales recuperables.

Error API no significa precio cero ni permite borrar línea automáticamente. M9
decide política temporal/caducidad, no modifica original. `captured_at` es guardado,
no garantiza frescura de API. Este módulo no implementa M9.

### M10: extensión fiscal

Regla actual: IVA único sobre subtotal MXN con tasa capturada, no motor fiscal.
Acordar cálculo por partida/exenciones/cargos, migraciones y versión de esquema
antes de cambiar cálculo. No recalcular emisiones con reglas nuevas; los documentos
afectados requieren nueva revisión. Un stub futuro no reemplaza M8 silenciosamente.

### M11: QA

Tests en `apps/quotes/tests/test_snapshots.py`: PostgreSQL/storage aislados,
concurrencia, originales inmutables y migración con legacy. Simulan commit exitoso
con respuesta perdida: el original debe sobrevivir. I11 sigue como caracterización
del flujo legacy sin M8; M7 debe migrar creación, no ocultar brecha cambiando test.

```bash
venv/bin/python manage.py test apps.quotes.tests.test_snapshots --noinput
venv/bin/python manage.py test --noinput
venv/bin/python -m unittest discover -s tools -p 'test_*.py' -v
venv/bin/python manage.py check
venv/bin/python manage.py makemigrations --check --dry-run
venv/bin/python manage.py migrate --check
git diff --check
```

PostgreSQL de pruebas con CREATEDB; no correr dos suites Django simultáneamente
contra misma BD de pruebas. Datos sintéticos, storage temporal, sin APIs reales.
Revisar visualmente PDF multipágina, unitarios largos, condiciones extensas y
etiquetas. Tests no certifican datos oficiales, logo ni condiciones reales.

### M13: despliegue y recuperación

1. Respaldar BD, aplicar `venv/bin/python manage.py migrate` y verificar.
2. `QUOTE_PDF_ROOT`: volumen privado persistente escribible por ASGI, con espacio
   y permisos restringidos. No efímero de contenedor ni públicamente servido.
3. Workers comparten BD/volumen. FileSystemStorage local; remoto requiere cambio explícito.
4. Login real, DEBUG=False, SMTP y datos comerciales validados.
5. Backups/restauración coordinados de BD/PDF. Recuperación: SDD §5.
6. Commit incierto: comprobar estado antes de purgar huérfanos. No borrar archivos
   en curso o referenciados. Revertir migración elimina tablas: requiere plan y autorización.

No hay jobs automáticos, Docker ni hardening global. Publicación/PR/merge y borrado
de rama requieren autorización separada.

## 7. Servicios comerciales v2 (P2)

Esquema/precisión/permisos: [24 §8](24-sdd-m8-snapshot-pdf.md#8-ampliación-comercial-v2--p1p2).
No sustituyen los servicios internos v1 de §§1–6. P4 conecta la API comercial en
[28 §13](28-m7a-integracion-vistas-y-m8.md#13-p4--api-comercial-v2-y-capturas-estables); UI pendiente.
P3 añade preview/documento/emisión v2 en servicios (§8), sin activar
COTIZACIONES_EMISION_HABILITADA ni certificar uso real.

### 7.1 Entrada tipada y escritura

Importar de `apps.quotes.snapshots`:

```python
key = uuid4()  # conservar la misma identidad en los reintentos
linea = PartidaComercialCapturada(
    name="Concepto sintético", source_unit_price=Decimal("100"),
    source_currency="MXN", quantity=Decimal("2"), line_key=key,
    source_kind="MANUAL", source_unit_label="Pieza", sale_unit_label="Pieza",
    pricing_mode="MARGIN", margin_percent=Decimal("35"),
)
revision = crear_borrador_comercial(
    quote_id, [linea], actor=actor_interno, company=empresa_validada,
    conditions=condiciones_cliente, request_id=uuid_estable,
)
```

El actor necesita acceso a Quote, lectura financiera y capacidad manual en ese
ejemplo. Las entradas API requieren source_kind="API" y proveedor/nombre/ID de la
captura confiable; no recibir precios de un JSON del navegador ni interpretar raw.
No usar las factories v1 heredadas como constructor comercial: aportar campos v2
explícitos (unidades, identidad, modo, bases IVA). P2 no implementa captura firmada.

| Servicio | Contrato |
| --- | --- |
| `crear_borrador_comercial` | Quote existente, partidas tipadas; actor/company/conditions obligatorios; client opcional; IVA16%, descuento0 explícitos al crear; request_id estable |
| `guardar_borrador_comercial` | revision_id + reemplazo completo; expected_version, company, conditions, client, tipo_cambio, exchange_context_id, tax_rate, discount_percent, issue_date, valid_days explícitos |
| `crear_revision_comercial` | origen v1 borrador/emitida o v2 emitida; partidas revisadas y actor/request_id/tax_rate/discount_percent/tipo_cambio/exchange_context_id explícitos; nuevo borrador, sin heredar constancias |
| `confirmar_advertencias_comerciales` | revision_id, pares (código, scope_ref), actor, expected_version y state_fingerprint que se mostraron |
| `validar_confirmaciones_comerciales` | revision_id, actor y expected_version; devuelve ResultadoComercial INTERNO; verifica, NO emite |

Crear admite tipo_cambio/identidad NULL para solo MXN, fecha/vigencia de Quote si
se omiten. Cuando hay FX, aportar TipoCambio y UUID de contexto estables, con origen
y fecha reales. Capturarlo inicialmente requiere capacidad FX. Para editar sin
permiso FX, conservar exactamente contexto y valores previos; cambiarlos requiere
capacidad y una identidad nueva. Un cambio de cabecera/cantidad no vuelve a buscar.

En manual con costo ausente: source_unit_price/source_currency/cost_tax_rate_included
son None, pricing_mode="MANUAL", margen NULL y precio/moneda manual definidos.
Con costo0, capturar Decimal("0") y moneda conocida. Precio manual corresponde a
unidad VENDIDA y no se divide otra vez por factor. Tasa incluida se expresa como
fracción (.16), NULL indica sin IVA; no inferirla de la tasa de venta.

Los importes resultantes son internos. Un actor sin facultad de ajustar un valor
puede conservar el valor previamente capturado al cambiar cantidad/cabecera. No
puede introducir un ajuste mediante cambios de identidad o confirmaciones.

### 7.2 Confirmar e invalidar

```python
confirmar_advertencias_comerciales(
    revision.pk, [("COSTO_DESCONOCIDO", str(line_key))],
    actor=asesor_autorizado, expected_version=revision.edit_version,
    state_fingerprint=revision.state_fingerprint,
)
resultado = validar_confirmaciones_comerciales(
    revision.pk, actor=actor_interno, expected_version=revision.edit_version,
)
# resultado puede contener costo/utilidad: no devolverlo directamente a un cliente.
```

Para avisos globales scope_ref="". No aceptar un booleano general ni escoger
confirmed_by desde HTTP. El servicio recalcula avisos y comprueba pertenencia,
permisos actuales y huella. Código/referencia inválidos → ValidationError; versión
obsoleta/huella diferente → RevisionConflict; falta de capacidad/confirmación válida
→ PermissionDenied. Repetir la misma aceptación no crea duplicados.

Después de editar: edit_version aumenta, state_fingerprint se recalcula y las
constancias anteriores quedan como antecedente, no autorización vigente. La huella
request_fingerprint inicial no cambia. Un confirmante inactivo o con permiso
revocado no habilita emisión; otra persona autorizada puede aceptar el estado actual.

### 7.3 Roles sin escalamiento técnico

`apps.quotes.permissions.configure_commercial_groups(actor=superusuario_activo)`
se ejecuta explícitamente DESPUÉS de migrate/post_migrate para preparar grupos.
No asigna miembros; primer admin comercial necesita asignación confiable autorizada.

`set_commercial_permission(user_id, capability, actor=admin_comercial, enabled=bool)`
solo gestiona los cinco ajustes permitidos de un asesor ya interno; no crea cuentas,
no da is_staff/superusuario, grupos, auth.change_user o gestión comercial. Consulta
usuarios frescos para no conservar una autorización revocada en has_perm.

P4 añade un endpoint limitado de administración comercial (doc28 §13), sin pantalla
de gestión de terceros en P5. No abrir UserAdmin genérico al asesor para suplirla.

### 7.4 Async, regresión y despliegue

Wrappers con thread_sensitive=True sobre la operación completa:
`acrear_borrador_comercial`, `aguardar_borrador_comercial`,
`acrear_revision_comercial`, `aconfirmar_advertencias_comerciales`.
No consultar relaciones lazy en event loop. P3 valida confirmaciones dentro de la
operación durable de emitir, no como comprobación independiente que una vista
pueda usar después sobre datos modificados.

La migración 0003 no contiene backfill ni asignación de usuarios. Crear el archivo
no significa haberlo aplicado a la BD de trabajo: pedir autorización, respaldar y
aplicar antes de ejecutar el código nuevo con esa BD. Tests: mismas instrucciones
PostgreSQL/storage aislados de §6, incluyendo suites comerciales nuevas.

Validación P2: **604 pruebas Django + 6 de terminal correctas**, con BD UUID
exclusiva creada/destruida, PDFs temporales, correo en memoria y red/SMTP reales
bloqueados. Incluye regresión v1 y forward/backward de migración con fixtures.
No se aplicó 0003 a la BD de trabajo ni se certificó API real/SMTP o emisión v2.

## 8. Consumo P3 · Preview y emisión comercial

Reglas completas: [24 §9](24-sdd-m8-snapshot-pdf.md#9-p3--preview-contexto-cliente-y-emisión-comercial-v2).

| Servicio | Contrato/resultado |
| --- | --- |
| `previsualizar_comercial(quote_id, partidas, ...)` | Mismos campos de nueva captura que crear_borrador_comercial, sin request_id; Quote accesible existente, no crea nada |
| `previsualizar_edicion_comercial(revision_id, partidas, ...)` | Mismos campos explícitos de guardar_borrador_comercial, con expected_version; no guarda cambios ni acepta avisos |
| `previsualizar_revision_comercial(revision_id, actor=..., expected_version=...)` | Solo borrador v2, lectura consistente; no requiere confirmar avisos |
| `emitir_revision(revision_id, actor=..., expected_version=...)` | V1/v2; bloqueos, validación comercial/confirmaciones v2, renderer cliente y sellado/archivo durable; reintento recupera original |
| `obtener_documento(folio, actor=..., number=..., allow_draft=...)` | Revisión exacta o preferencia histórica por última emitida; v2 cliente sin datos internos; emitida solo metadatos+bytes archivados |

### 8.1 Separar preview interno y salida cliente

```python
preview = previsualizar_revision_comercial(
    revision_id, actor=asesor_interno, expected_version=version_leida,
)
finanzas_internas = preview["resultado"]  # costos/utilidad/avisos, NO salida cliente
contexto_cliente = preview["cliente"]    # allowlist apta para el renderer
pdf_borrador = generate_quote_pdf(contexto_cliente)  # temporal, no emisión
```

`persistida=True` identifica lectura de borrador guardado. En simulación de nueva
captura es False, edit_version/revision son None; en edición propuesta es False,
edit_version es la BASE y base_state_fingerprint su huella guardada. No asigna una
versión nueva ni acepta confirmaciones de una huella propuesta. Guardar, recargar y
confirmar usando versión/huella guardadas antes de emitir.

Precio y costos originales llegan de selección API confiable/captura manual
autorizada; no son un contrato JSON para aceptarlos del browser. Nada de llamar a
proveedores desde preview, editar o emitir. La simulación de edición conserva ajustes
previos sin exigir permiso de volver a introducirlos, pero sí lo exige al cambiarlos.

El contexto cliente excluye proveedor/costo/moneda de origen/FX/stock/margen/utilidad.
Solo marca/modelo de producto, código externo al cliente validado, unidad/cantidad y
venta MXN; no copiar external_id de distribuidor ni notas internas a textos públicos.
Los textos libres deben validarse por intención comercial en el consumidor: esta
allowlist no elimina secretos que un usuario escriba en una descripción/condición.

### 8.2 Emitir y recuperar

```python
# Primero confirmar TODOS los avisos del borrador guardado según §7.2.
emitida = emitir_revision(
    revision_id, actor=asesor_interno, expected_version=version_leida,
)
contexto, original, filename = obtener_documento(
    folio, actor=asesor_interno, number=emitida.number, allow_draft=False,
)
# original son los bytes archivados verificados. No llamar al renderer otra vez.
```

No envolver emitir_revision en atomic real ni enviar SMTP dentro. Se validan huella,
versión, acceso/capacidades y confirmantes vigentes dentro de la transacción durable.
Emisor/confirmantes User quedan bloqueados contra el gestor de revocaciones hasta
el commit. La emisión guarda sale_amount/cost_amount, resultados globales y PDF;
amount v1 y los originales capturados no se reinterpretan ni se sobrescriben.

Una vez emitida, recuperar original/reintentar NO pide confirmar de nuevo ni ejecuta
el motor. Metadatos v2 tienen archived_only=True y los unitarios son los del PDF,
no de un cálculo actual. El renderer rechaza esos metadatos para impedir usar esa
lectura como regeneración. Preview de emitida se rechaza: consultar el original.
Avisos/confirmaciones siguen accesibles como auditoría interna, no se envían al cliente.

La presentación usa 2–12 decimales adaptativos y `(aprox.)` cuando el unitario se
acorta; importes definitivos a centavos. El límite12 es visual, no precisión guardada.
El PDF explica la diferencia y presenta descuento global antes de IVA sin reparto.

### 8.3 Async y límites de esta entrega

Wrappers materializados con thread_sensitive=True:
`aprevisualizar_comercial`, `aprevisualizar_edicion_comercial`,
`aprevisualizar_revision_comercial`; emisión y documentos usan los existentes
`aemitir_revision` / `aobtener_documento`. No hay un segundo renderer ni endpoints
duplicados. API comercial P4 documentada en doc28 §13, UI P5 en doc28 §14; no habilitar mocks ni emisión
HTTP por asumir que tener servicios equivale a despliegue aceptado.

Pruebas nuevas amplían test_commercial_persistence.py/test_commercial_permissions.py:
simulación sin INSERT/UPDATE/DELETE, edición propuesta, huellas/versiones, privacidad
de contexto/PDF, redondeo periódico, avisos/costos ausentes/total0, revocación,
emisión/edición/preview concurrentes, usuarios bloqueados, fallos de renderer/storage/
commit incierto, descarga/correo con bytes idénticos y conservación de originales v1.

Validación P3: **640 pruebas Django + 6 terminal correctas**, recursos UUID/PDF/correo
aislados y APIs/SMTP reales bloqueados. 0003 no aplicada a BD de trabajo; uso real no habilitado.

## 9. Frontera HTTP P4

Contrato vigente y ejemplos en [28 §13](28-m7a-integracion-vistas-y-m8.md#13-p4--api-comercial-v2-y-capturas-estables).
0004 añade QuoteSelectionContext (captura normalizada, nunca raw) y QuoteAPIRequest
(recibo de intención transaccional). P4 no cambia defaults históricos ni aplica
migraciones a BD de trabajo. No sustituir originales API con costos del JSON.

Vista previa nueva sin Quote: `previsualizar_nueva_cotizacion` recibe actor interno,
empresa de servidor/cliente/condiciones y partidas verificadas; ancla efímera sin
folio ni escritura. No usar folio inventado ni crear Quote escondida para autorizar.
HTTP llama a servicios completos desde hilo ORM, no a helpers sin autorización en
event loop. Simulación de edición continúa ligada a revisión/versión base.

La emisión admite `state_fingerprint` opcional en servicio para compatibilidad P3;
en HTTP P4 es OBLIGATORIO y se valida bajo bloqueo durable junto con edit_version.
V1 queda consultable; nueva emisión HTTP v1 y PUT comercial v1 se rechazan409 y
requieren transición explícita. No truncar cantidades ni asignar margen histórico.

La base IVA API verificada se configura explícitamente en servidor; por defecto
UNKNOWN impide crear/preview comercial de esa selección. Ningún proveedor real
queda certificado por las pruebas sintéticas. Emisión HTTP continúa OFF.

## 10. Consumidor de interfaz P5

La pantalla interna de cotizador consume el adaptador comercial conectado sin cambiar
el flag global de simulaciones. No calcula precios en JavaScript ni conserva datos
financieros en almacenamiento del navegador. Edita snapshots con EXISTING, conserva
versión/huellas e intención ante guardado incierto y confirma solo versiones guardadas.
Detalle FX añade valor/origen/fecha capturados; capacidades identifica actor_id para
detectar cambios de sesión antes de reenviar una intención. No hay esquema nuevo.

Distribución de controles, recuperación y QA en [28 §14](28-m7a-integracion-vistas-y-m8.md#14-p5--cotizador-conectado-al-cálculo-comercial).
La UI no habilita emisión, migraciones o roles reales. Resumen/PDF/correo exactos se
conectan en P6; no reemplazar originales ni navegar a un mock como resultado real.
