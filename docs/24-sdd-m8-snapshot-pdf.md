# 24 · SDD M8: revisiones, snapshots y PDF original

## 1. Alcance y decisiones

Implementación en `feature/quotes-m8-snapshot-pdf`, autorizada el 2026-10-12.
Fuente de verdad: `apps/quotes/models.py`, `snapshots.py`, `snapshot_values.py`,
`storage.py` y `pdf.py`. Contrato de consumo: [25 · Integración](25-m8-snapshot-pdf-integracion.md).

M8 añade versiones a `Quote`, sin reemplazar `QuoteItem`, modificar `Product`
ni convertir filas históricas por suposición. Captura originales al guardar el
borrador; al emitir congela importes y archiva el PDF. Una corrección del cliente
requiere otra revisión, nunca sobrescribir el documento emitido.

| Decisión | Motivo |
| --- | --- |
| `QuoteRevision` + `QuoteSnapshot` por partida | Separar cabecera/FX y datos de línea sin duplicación |
| Capturar al guardar, sellar al emitir | La API/catálogo pueden cambiar entre ambas operaciones |
| No pasar originales por `Product`/`QuoteItem` | Sus precios de dos decimales perderían precisión |
| MXN de salida, USD/MXN de origen, FX explícito | No mezclar monedas ni asumir una tasa |
| Precio 6 decimales / FX 8 con validación exacta | Conservar precisión aprobada sin redondeo silencioso |
| Borradores regenerables, emisiones archivadas | Menos archivos innecesarios y originales recuperables |
| Storage privado + SHA-256 | Reutilizar los mismos bytes y detectar archivos dañados |
| Único renderer ReportLab | No duplicar reglas de presentación |
| Histórico etiquetado y demo explícito | No inventar procedencia ni reemplazar errores con datos falsos |

No incluye UI de edición, nuevos endpoints de creación/emisión, recotización por
API, caché/obtención de FX, márgenes, descuentos, flete, impuestos por partida,
facturación, auditoría de envío/aceptación ni almacenamiento remoto.
Todo sigue en el monolito, sin dependencias nuevas ni build.

## 2. Modelo persistido

Migración aditiva: `apps/quotes/migrations/0002_quoterevision_quotesnapshot.py`.
No migra datos ni rellena moneda/FX para filas anteriores.

### `quotes_quoterevision`

| Campo | Tipo / regla |
| --- | --- |
| `quote` | FK PROTECT → Quote; `related_name="revisions"` |
| `number` | Entero positivo, único dentro de Quote, asignado bajo bloqueo |
| `based_on` | FK opcional PROTECT → revisión emitida de la misma Quote |
| `request_id` | UUID único dentro de Quote; clave de reintento de creación |
| `request_fingerprint` | SHA-256 de solicitud inicial normalizada, no del estado editado |
| `edit_version` | Empieza en 1; aumenta por reemplazo de borrador |
| `status` | DRAFT o ISSUED; no representa envío ni aceptación |
| `schema_version` | 1; el contexto rechaza versiones desconocidas |
| `header` | JSON de folio, empresa/asesor, cliente y condiciones |
| `currency` | MXN, restringida en BD |
| `exchange_rate` | numeric(18,8), nulo sin conversión; MXN por USD |
| `exchange_rate_source` / `exchange_rate_date` | Origen de hasta 120 caracteres y datetime aware; requeridos con FX |
| `tax_rate` | numeric(5,4), entre 0 y 1 |
| `issue_date` / `valid_days` | Fecha comercial explícita; vigencia 1–3650 días en servicios |
| `subtotal` / `tax` / `total` | numeric(38,2), nulos en DRAFT, congelados en ISSUED |
| `pdf` | Ruta privada del original, vacía en DRAFT |
| `pdf_sha256` / `issued_at` | Huella y fecha real de sellado, vacías/nula en DRAFT |
| `created_at` / `updated_at` | Fechas de persistencia |

Cabecera JSON versión 1; valores textuales excepto la lista:

```text
folio
company: name, rfc, tagline, phone, email, advisor
client: business_name, rfc, contact, email, phone, project
conditions: lista de textos
```

`company.name` y `client.business_name` deben ser no vacíos. Los demás campos
pueden estar vacíos; no se inventan contactos/RFC. Máximo 500 caracteres por campo
de empresa/cliente y 50 condiciones de hasta 6000 caracteres cada una. Empresa y
condiciones son explícitas, no se copian del demo. El asesor se captura del usuario
propietario; al clonar se captura el asesor actual, sin cambiar el documento anterior.

### `quotes_quotesnapshot`

| Campo | Tipo / regla |
| --- | --- |
| `revision` | FK PROTECT → QuoteRevision; `related_name="snapshots"` |
| `position` | Entero positivo, único dentro de revisión; conserva orden |
| `code` / `name` / `description` | Texto capturado, sin referencias al catálogo vigente |
| `provider_code` / `provider_name` / `external_id` | Procedencia comercial; código 80 y nombre 255 caracteres |
| `brand` / `model` / `mpn` | Identificadores originales, independientes de normalización M5 |
| `source_unit_price` | numeric(18,6), precio original no negativo |
| `source_currency` | USD o MXN, explícita también para conceptos manuales |
| `quantity` | numeric(10,2), positiva |
| `stock` | Entero no negativo o NULL; 0 y NULL son distintos |
| `es_mejor_precio` | Marca M6 por proveedor; no implica disponibilidad/equivalencia |
| `amount` | numeric(38,2), nulo en borrador; MXN congelado al emitir |
| `captured_at` | Momento de guardado de esa partida, no fecha de consulta de API |

No guarda `raw`, credenciales ni tokens. Los servicios limitan los textos restantes
de partida a 6000 caracteres; `name` no puede estar vacío. Una oferta con proveedor
requiere código, nombre e ID externo coherentes. Un concepto manual puede omitir
proveedor/ID, pero debe declarar precio/moneda. No hay FK a Product, Provider o
ProductoCanonico: sus cambios no alteran snapshots; no se infiere matching.

### Restricciones y frontera de integridad

La BD restringe unicidad de revisión/solicitud/posición, monedas, valores positivos,
IVA y coherencia de cabecera sellada. `save()` valida decimales antes de guardarlos
y bloquea cambios sobre emisiones. Managers rechazan `update`, `delete`,
`bulk_create` y `bulk_update`; admin de solo lectura. No se borran revisiones ni
se mueven partidas entre revisiones.

**Los servicios son la única interfaz de escritura soportada.** No hay triggers
de inmutabilidad: SQL directo, `save_base`, managers alternativos o acceso al
disco pueden saltarse las garantías. `_seal` es interno, no API ni autorización.
M13 debe restringir permisos BD/volumen. SHA-256 no es firma ni certificado fiscal.

## 3. Precisión y totalización

| Valor | Rango admitido |
| --- | --- |
| Precio original | 0 a 999999999999.999999 |
| FX | 0.00000001 a 9999999999.99999999 |
| Cantidad | 0.01 a 99999999.99 |
| IVA | 0 a 1, máximo 4 decimales |
| Importe/subtotal/IVA/total | 0 a 10^36 − 0.01 |
| Partidas | 1–500 por revisión |
| Stock | NULL o entero 0–9223372036854775807 |

Entradas `Decimal`, nunca float ni texto parseado implícitamente. Se rechazan
NaN/infinito, magnitud excesiva y precisión significativa sobrante antes de guardar.
Se pueden eliminar ceros finales sin cambiar valor: `85.500000000` es válido,
`85.5000001` no. M6 puede recibir FX/precios de mayor precisión; M8 no los redondea
para hacerlos caber.

```text
factor = 1 si origen MXN, FX explícito si USD
unitario_MXN = precio_origen × factor                  # sin redondear
importe_partida = ROUND_HALF_UP(unitario_MXN × cantidad, 0.01)
subtotal = suma de importes ya redondeados
IVA = ROUND_HALF_UP(subtotal × tasa_IVA, 0.01)
total = subtotal + IVA
```

Contexto Decimal local de precisión 64, independiente de traps/precisión del
consumidor. Los máximos combinados con 500 líneas caben en importes numeric(38,2).
No usar `Quote.subtotal` al sellar: el legacy redondea el agregado, no cada partida.
El PDF muestra unitarios convertidos hasta 14 decimales (6 + 8), sin ceros finales
innecesarios, y precio/moneda de origen en descripción; importes/totales a dos.
Si el unitario completo no cabe en columna, esta indica «Ver detalle» y el valor
exacto se muestra en descripción, sin truncarlo ni partir dígitos para hacerlo caber.

La fecha comercial/vigencia del borrador no cambia automáticamente al emitir;
`issued_at` sí registra sellado real. Los precios deben tener base compatible sin
IVA incluido. M8 no detecta IVA de proveedor ni convierte costo a precio de venta.
Las condiciones reales de Exel siguen pendientes de verificación.

## 4. Ciclo de vida, permisos y concurrencia

1. Crear Quote por la vía del equipo y capturar borrador con originales explícitos.
2. `guardar_borrador` reemplaza cabecera/partidas en una transacción usando
   `expected_version`; una edición obsoleta produce conflicto.
3. Descargar borrador genera PDF **BORRADOR — SIN EMITIR**, sin escribir archivo.
4. Emitir calcula/genera/guarda/verifica PDF, congela importes y sella. Reintentar la
   misma versión devuelve emisión existente y verifica su archivo, sin regenerarlo.
5. Descarga/correo de emisión leen archivo y verifican SHA-256.
6. `crear_revision` origina otro DRAFT con número nuevo y `based_on`, sin PDF ni
   totales sellados. Sus cambios no modifican la revisión origen.

Servicios públicos: actor autenticado/activo, propietario de Quote o `is_staff`.
Admin requiere además permisos Django de lectura. `Quote.status` solo pasa de
DRAFT a ISSUED; no retrocede SENT/ACCEPTED. Correo no cambia estado de revisión
ni crea auditoría de envío. El ciclo comercial sigue siendo trabajo posterior.

En PostgreSQL se bloquea Quote y después revisión. Quote serializa numeración y
claves; revisión protege cabecera/partidas/emisión. La lectura materializa contexto
consistente bajo esos bloqueos, sin mezclar versiones. La descarga renderiza/lee
después de liberar la transacción. SQLite no ofrece estas garantías de concurrencia.

Emisión con `atomic(durable=True)`: fuera de cualquier otra transacción real,
incluido `ATOMIC_REQUESTS`. No envolver emisión + SMTP en una atomic. Wrappers
async ejecutan el servicio completo fuera del event loop; no consultar relaciones
ORM lazy del resultado en vistas async.

## 5. Archivo privado, fallos y recuperación

`QUOTE_PDF_ROOT` selecciona volumen privado; predeterminado
`BASE_DIR/private_quote_files`. Ruta `quotes/<quote_id>/r<number>/<uuid>.pdf`,
sin nombres de personas/clientes. Archivos/directorios nuevos con modos 0600/0700;
proteger también permisos del volumen existente. El storage no tiene URL pública.
No mapearlo con `static()`, MEDIA_URL, nginx público o bucket público. El directorio
predeterminado está excluido de Git; excluir también otra ruta elegida dentro del repo.

PostgreSQL y filesystem **no son una única transacción**:

| Fallo | Comportamiento |
| --- | --- |
| Validación/renderer | No marca emitida ni escribe original |
| Storage antes de completar save | No marca emitida; escritura parcial puede dejar huérfano |
| Verificación/persistencia antes de commit | Rollback; limpia solo archivo nuevo cuyo save terminó |
| Limpieza falla/proceso muere | Puede quedar huérfano privado |
| Commit falla o resultado incierto | Llamada falla; conserva archivo porque BD podría haber confirmado |
| Original ausente/hash distinto tras emisión | Error controlado, no regeneración/demo ni borrado de emisión |

Se verifica el archivo escrito antes de sellar. El servicio solo devuelve éxito
tras commit confirmado. No garantiza sobrevivir fallos físicos sin backups. Los
PDF emitidos consumen disco; RAM solo durante render/lectura/envío, no permanentemente.

### Procedimiento operativo

1. Tras commit incierto, esperar que termine y consultar la revisión con conexión
   sana. Si está ISSUED, conservar/verificar original; reintento misma revisión y
   versión es idempotente. Si DRAFT, reintentar servicio, no sellar mediante SQL.
2. Revisar huérfanos comparando inventario privado con rutas `pdf` referenciadas.
   No borrar durante emisiones: un archivo aún sin commit no es necesariamente
   huérfano. Pausar escrituras o esperar periodo de gracia y verificar de nuevo.
3. Para original ausente/corrupto, restaurar **bytes exactos** del respaldo y
   comprobar hash. No sustituir por regeneración. Sin backup, registrar pérdida
   y decidir comercialmente si se necesita una nueva revisión.
4. Respaldar y probar restauración coordinada de PostgreSQL y volumen PDF.
   No retirar volumen ni revertir migración con emisiones existentes.

No hay purga/recuperación automática. Quien puede alterar BD y archivo puede
recalcular huella: acceso restringido y backups son indispensables.

## 6. Histórico, ausencia y demo

- Quote sin revisiones: `get_quote_context` carga legacy real y marca
  **DOCUMENTO HISTÓRICO — SIN SNAPSHOT DE ORIGEN**. Mantiene cantidades/precios/
  totales originales; moneda/FX no registrados. Membrete actual no validado como
  histórico, con advertencia. Se regenera, no es original M8 ni historial inmutable.
- Quote con revisiones: usar `obtener_documento`; `get_quote_context` rechaza esa vía.
- Folio/revisión ausente: 404. Fallo operacional: 500/503; nunca demo automático.
- Demo: `build_demo_quote` y `/demo/cotizaciones/<folio>/pdf/`, solo DEBUG, con
  **DEMO — SIN VALIDEZ COMERCIAL**. No crea filas ni se envía por endpoint oficial.
  Los ejemplos de membrete no se incorporan a nuevos snapshots.

El logo se incrusta al emitir; cambiarlo o cambiar renderer no altera original,
pero sí borradores regenerados. Antes de uso real CCONOR debe validar logo,
datos comerciales, condiciones y remitente SMTP.

## 7. Verificación

`apps/quotes/tests/test_snapshots.py`: límites, FX M6, redondeo por línea, captura/
edición/clonado, inmutabilidad, permisos, descarga/correo, privacidad, multipágina/
escape de marcado, archivos dañados, rollback, commit incierto, migración legacy,
wrappers async y concurrencia real PostgreSQL. Datos sintéticos y storage temporal.
Comandos y responsabilidades: [25](25-m8-snapshot-pdf-integracion.md).

Validación local 2026-10-12: 448 pruebas Django (50 M8 nuevas) y 6 de terminal
aprobadas; checks, consistencia de migraciones, enlaces y diff sin errores.
Migración aditiva aplicada en PostgreSQL local. PDF sintético revisado visualmente,
incluyendo multipágina y unitarios largos; no sustituye validación comercial CCONOR.
