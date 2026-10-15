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
