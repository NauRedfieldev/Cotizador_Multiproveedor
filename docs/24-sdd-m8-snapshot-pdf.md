# 24 · SDD M8: revisiones, snapshots y PDF original

**Versionado:** §§1–7 describen el esquema histórico v1. La ampliación comercial
v2/P2 está en §8; preview/render/emisión de servicio P3 en §9. API P4 en
[28 §13](28-m7a-integracion-vistas-y-m8.md#13-p4--api-comercial-v2-y-capturas-estables); UI P5 en [28 §14](28-m7a-integracion-vistas-y-m8.md#14-p5--cotizador-conectado-al-cálculo-comercial).
Resumen/documentos P6 en [28 §15](28-m7a-integracion-vistas-y-m8.md#15-p6--mis-cotizaciones-resumen-y-documentos-exactos).
No reinterpretar los
campos históricos usando reglas comerciales nuevas.

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

## 8. Ampliación comercial v2 · P1/P2

Código: `commercial.py`, `models.py`, `permissions.py`, servicios comerciales en
`snapshots.py`. Migración: `0003_commercial_revision_v2`. Consumo: [25 §7](25-m8-snapshot-pdf-integracion.md#7-servicios-comerciales-v2-p2).

### 8.1 Alcance implementado y límites

- Motor puro con Decimal: margen sobre venta, venta manual, unidades enteras/factor,
  captura con/sin IVA, USD/MXN con FX explícito, descuento porcentual GLOBAL antes
  de IVA y utilidad bruta completa/incompleta. Sin reparto del descuento a partidas.
- Persistencia explícita de borradores v2, edición con versión, transición/clonado
  con entradas revisadas, permisos comerciales y constancias de confirmación.
- No cambia el default `schema_version=1` de consumidores anteriores. No migra
  datos comerciales ni asigna margen, unidades, moneda o FX al histórico.
- Al cerrar P2, emisión/documento comercial v2 estaban bloqueados. **P3 (§9)**
  conecta motor, confirmaciones, sellado y renderer cliente a nivel de servicio.
  P4 conecta los endpoints (doc28 §13); P5 conecta el cotizador (doc28 §14). Uso real pendiente.
- Los resultados del motor y de la validación P2 son INTERNOS, contienen costos:
  no serializarlos directamente al cliente ni ponerlos en el documento externo.

Flujo v2, con valores originales preservados y sin redondear unitarios intermedios:

```text
costo_unitario_MXN = normalizar_IVA_y_moneda(costo_original) / factor_conversion
venta_unitaria_MXN = costo_unitario_MXN / (1 - margen/100)
    O normalizar_IVA_y_moneda(venta_manual_por_unidad_vendida)
importe_venta = HALF_UP(venta_unitaria_MXN * cantidad_entera, .01)
importe_costo = HALF_UP(costo_unitario_MXN * cantidad_entera, .01), si se conoce
sale_subtotal = suma de importes_venta
discount_amount = HALF_UP(sale_subtotal * discount_percent/100, .01)
subtotal = sale_subtotal - discount_amount
tax = HALF_UP(subtotal * tax_rate, .01)
total = subtotal + tax
gross_profit = subtotal - suma_costos, solo si TODOS los costos se conocen
```

Defaults para entradas NUEVAS: margen35%, IVA16%, descuento0. Se centralizan en
`commercial.py`; el contrato capturado v2 exige modo/margen explícitos y no inventa
el 35% si faltan. Instalación/flete se representan como manual sin margen automático;
aplicarlo es elección autorizada explícita. El modo margen no admite 100% ni negativos
(máximo99.99 con 2 decimales); descuento sí admite100%, con aviso TOTAL_CERO y
  confirmación autorizada al emitir. Costo ausente exige venta manual y aviso.

### 8.2 Cabecera `QuoteRevision`

Todos los campos siguientes son NULL en filas v1; no hay backfill comercial.

| Campo | Tipo/regla v2 |
| --- | --- |
| `calculation_version` | Entero pequeño; actualmente 1, identidad inmutable |
| `state_fingerprint` | SHA-256 del estado normalizado actual, actualizable solo al editar borrador |
| `exchange_context_id` | UUID explícito con FX; NULL si no hay FX |
| `discount_percent` | numeric(5,2), 0–100; no importe fijo; default0 solo al crear v2 |
| `sale_subtotal` | numeric(38,2), suma de ventas de partidas antes del descuento |
| `discount_amount` | numeric(38,2), descuento calculado sobre esa suma |
| `known_cost_total` | numeric(38,2), suma de costos conocidos; puede ser parcial |
| `costs_complete` | Booleano: todos los costos conocidos; no equivale a suma distinta de0 |
| `gross_profit` | numeric(38,2), firmado: subtotal neto menos costo total; NULL si incompleta |

En v2 `subtotal` es el neto antes de IVA: `sale_subtotal - discount_amount`;
`tax`/`total` conservan sus sentidos. En v1 no cambia su cálculo histórico.
Todos los RESULTADOS nuevos y `subtotal/tax/total` son NULL en DRAFT. Las entradas
comerciales sí se capturan; calcular preview no significa sellar. Se permite
guardar costo ausente; su representación persistida es NULL, nunca un cero supuesto.

La huella de creación `request_fingerprint` no se modifica al editar. La huella
`state_fingerprint` incluye cabecera, partidas normalizadas, FX/identidad, fecha/
vigencia, IVA, descuento y versiones de esquema/cálculo. No incluye aceptación ni
timestamps de auditoría, evitando ciclos e idempotencia dependiente del reloj.
Las fechas FX se canonizan a UTC para que guardar en PostgreSQL no cambie la huella.

### 8.3 Partida `QuoteSnapshot`

| Campo | Tipo/regla v2 |
| --- | --- |
| `line_key` | UUID estable, único dentro de revisión; distinto de `position` o PK física |
| `source_kind` | API o MANUAL; NULL exclusivamente en estructura v1 |
| `source_unit_price` / `source_currency` | Ahora nullable; NULL juntos solo para costo manual desconocido |
| `cost_tax_rate_included` | numeric(5,4), NULL=neto; tasa explícita si el precio capturado contenía IVA |
| `source_unit_label` / `sale_unit_label` | Hasta 80 caracteres, no vacíos; unidades explícitas |
| `conversion_factor` | numeric(18,6), positivo; divisor del costo de origen por unidad vendida |
| `unit_validated_by` / `unit_validated_at` | FK usuario PROTECT y fecha aware del actor real que valida unidades |
| `pricing_mode` | MARGIN o MANUAL, excluyentes |
| `margin_percent` | numeric(4,2), 0–99.99, obligatorio solo en MARGIN |
| `manual_unit_price` / `manual_currency` | numeric(18,6) y MXN/USD; requeridos en MANUAL, incluido precio0 |
| `manual_tax_rate_included` | Tasa incluida en venta manual, independiente del IVA de venta de cabecera |
| `sale_amount` / `cost_amount` | numeric(38,2), NULL en borrador; destino de sellado P3, costo NULL si desconocido |

`amount` queda reservado a v1 y debe ser NULL en v2. No hay descuento ni utilidad
DESPUÉS del descuento por partida. El motor etiqueta sus indicadores individuales
como `*_antes_descuento` para no atribuirles un descuento que no se distribuye.

Cantidad sigue almacenada en numeric(10,2) por compatibilidad; v2 exige entero
positivo y rechaza fracciones, sin truncarlas. V1 conserva cantidades decimales.
Precios originales y FX siguen con validación exacta de §3. Derivados periódicos
no se cuantizan a seis decimales: el motor redondea el cociente exacto a centavos
después de multiplicar cantidad. `as_integer_ratio/divmod` evita sesgar HALF_UP por
un unitario truncado (por ejemplo .025 / 3 × 3 debe dar .03).

- API: proveedor/nombre/ID coherentes, costo y moneda conocidos. Su captura debe
  venir del consumidor confiable; P2 no prueba una API ni firma payloads del browser.
- MANUAL: no simula identificadores de una API. Puede desconocer costo, pero entonces
  no declara moneda/tasa ficticias y necesita una venta manual definida.
- MARGIN: exige costo conocido y margen; no recibe campos de venta manual.
- MANUAL: margen NULL y venta/moneda conocidas. Costo0 conocido no es costo ausente.
- Capturar importe con IVA conserva bruto/tasa originales; no sobrescribirlos con
  neto redondeado ni asumir que cualquier proveedor entrega precio sin IVA.

### 8.4 Integridad y ciclo de escritura

Checks SQL cubren forma v1/v2 local, presencia costo/moneda, modos de venta, rangos,
cantidad entera v2 y coherencia de importes/huellas/versiones. Las ramas de campos
obligatorios comprueban `IS NOT NULL`: un CHECK con comparación NULL podría aceptar
UNKNOWN en PostgreSQL. Un CHECK no consulta la tabla padre: servicios/modelos validan
además schema_version/estado de revisión, autorización, pertenencia y unidad/FX.
Se mantiene la frontera de §2: no hay triggers que impidan SQL privilegiado.

Edición v2 conserva la captura original API: cambiar su precio, moneda, base IVA,
identidad o stock bajo el mismo line_key produce conflicto. Descripción/cantidad
pueden editarse sin consultar a proveedores. Una selección nueva es una intención
explícita; el refresco por API sigue fuera de este módulo. Los conceptos manuales
son entradas distintas y requieren capacidad manual al crearlos/ajustarlos.

Guardar reemplaza filas físicas, pero conserva line_key, captured_at y validación
de unidades si estas no cambian. Aumenta edit_version y cambia state_fingerprint;
no cambia request_fingerprint. Los bloqueos conservan orden Quote→revisión.

Transición v1→v2 crea OTRA revisión con based_on, entradas comerciales explícitas y
revisadas; puede partir de borrador v1 o emisión. No edita schema_version ni redondea
cantidades antiguas para adaptarlas. Los originales/archivos de origen no cambian.

### 8.5 Permisos comerciales y grupos

| Permiso `quotes.*` | Función |
| --- | --- |
| `view_commercial_costs` | Identificación/lectura financiera interna, además del acceso a la Quote; confirmar STOCK_DESCONOCIDO |
| `change_commercial_margin` | Ajustar margen y confirmar MARGEN_CERO |
| `set_commercial_sale` | Captura/venta manual; confirmar costo desconocido, venta manual bajo costo o venta0 |
| `change_commercial_tax` | Cambiar IVA y tasas incluidas; la captura inicial de la tasa estándar no la cambia |
| `change_commercial_fx` | Capturar/cambiar FX e identidad de contexto explícitos |
| `apply_commercial_discount` | Aplicar descuento y confirmar pérdida GLOBAL o total0 causado por descuento |
| `manage_commercial_permissions` | Gestionar SOLO los cinco ajustes anteriores |

`configure_commercial_groups` es bootstrap técnico explícito, posterior a
migrate/post_migrate, solo por superusuario activo. Crea/configura Personal CCONOR
(lectura) y Administrador CCONOR (lectura+ajustes+gestión), **sin añadir miembros**.
No se ejecuta al importar ni en RunPython; no presume que los permisos existen
antes de post_migrate. Asignar el primer administrador requiere operación autorizada.

El gestor no añade usuarios a grupos ni otorga lectura interna, gestión, auth.change_user,
is_staff o superusuario. Rechaza cambios en permisos heredados de roles para no fingir
una revocación inefectiva. Usa filas User bloqueadas en orden por PK y comprueba
capacidades con un usuario fresco, evitando cachés de permisos tras revocación.
No hay interfaz/endpoint de gestión comercial aún: se añadirá en la entrega de UI/API.

El rol por sí solo no abre cotizaciones ajenas: se conserva propietario/staff de
`authorize`. Staff técnico no implica capacidad comercial; un admin comercial no
necesita is_staff. El admin de snapshots/constancias sigue de solo lectura y exige
además permiso financiero interno y permiso Django de consulta del modelo.

### 8.6 Constancias y emisión futura

`quotes_quotecommercialconfirmation`: revision PROTECT, edit_version, state_fingerprint,
warning_code, scope_ref (UUID de línea o vacío global), confirmed_by PROTECT y fecha
servidor aware. Unicidad por esos datos más actor permite reintentos idempotentes y
otra aceptación válida por otro actor sin sobrescribir una constancia anterior.

Confirmar exige un aviso vigente calculado por el motor, estado/versión/huella
actuales, acceso interno a Quote y permiso correspondiente. No acepta código o
partida arbitrarios. La constancia es inmutable y no cambia edit_version. Editar
los datos deja anteriores constancias conservadas pero obsoletas; clonar no las copia.

`validar_confirmaciones_comerciales` recalcula estado, comprueba la huella y exige
constancia para CADA aviso vigente que requiere confirmación. Reconsulta actividad, permiso y acceso actual
del confirmante. Una revocación exige otra aceptación autorizada. TOTAL_CERO por
descuento exige permiso de descuento; sin descuento, capacidad de venta manual.
Un borrador pendiente puede guardarse sin aceptar aún.

Avisos de stock (C16, solo partidas `source_kind=API`; no cambian importes ni huella).
Se comparan en unidad vendida: `stock × conversion_factor` frente a la cantidad.

| Código | Condición | Efecto |
| --- | --- | --- |
| `STOCK_CERO` | El proveedor informó 0 | Informativo: no se acepta ni bloquea emitir |
| `STOCK_INSUFICIENTE` | `0 < stock × factor < cantidad` | Informativo: no se acepta ni bloquea emitir |
| `STOCK_DESCONOCIDO` | Stock no informado (`None`, no es 0) | Requiere confirmación (comprobación del asesor) |

Confirmar un aviso informativo se rechaza y no crea constancia. Conceptos manuales no
evalúan stock. El cliente no ve avisos de stock por partida; el PDF comercial lleva siempre
la leyenda fija «Precios y disponibilidad sujetos a confirmación al momento del pedido.»
(`LEYENDA_DISPONIBILIDAD` en `pdf.py`, docs/30 B2; solo emisiones nuevas). Migración
0005 solo amplía las opciones de `warning_code` (sin SQL en PostgreSQL).

Confirmar/validar por sí solos
NO emiten: P3 integra la misma validación dentro de la transacción durable (§9), sin
reducir garantías de archivo/commit de §5 ni regenerar originales existentes.

### 8.7 Pruebas y operación

`test_commercial.py` cubre el motor puro; `test_commercial_persistence.py` cubre
borradores, NULL/0, controles SQL, versión, UTC FX, constancias y migración aislada;
`test_commercial_permissions.py` cubre capacidades, cachés, roles y admin de lectura.
Se conserva toda regresión v1, incluida migración legacy; el test de migración v1
restaura las hojas actuales para no dejar la BD temporal en 0002.

La migración 0003 se prueba hacia delante/atrás solo con fixtures aislados. Revertirla
después de usar v2 en datos reales perdería datos/constancias y no es rollback seguro.
El código nuevo requiere 0003 antes de usar sus modelos contra una BD: no se aplica
automáticamente. No confundir pruebas UUID con migración de la BD de trabajo.

## 9. P3 · Preview, contexto cliente y emisión comercial v2

- Proveedor elegido: únicamente interno, no mostrarlo en el documento/salida cliente.
- Tipo de cambio: conservar como evidencia interna; no mostrarlo al cliente.
- Mantener costos, porcentajes y utilidad fuera del contexto cliente. Marca/modelo
  de producto no equivale al distribuidor elegido; no añadir procedencia interna.
- Precio unitario periódico: presentación aprobada e implementada, sin alterar la regla
  acordada de calcular partida con precisión y redondear el importe final. Ejemplo
  sintético: unitario 33.333333… × 3 → importe 100.00; mostrar solo 33.33 puede hacer
  que parezca 99.99. Mostrar más decimales cuando haga falta y leyenda de unitario
  aproximado; importes/totales a dos decimales. No se eligió una precisión fija.
- Los originales v1 siguen inmutables. La nueva presentación corresponde a v2:
  no regenerar v1 para eliminar datos ni tratarlos como salida comercial saneada.

### 9.1 Preview y separación de información

Servicios tipados, sin HTTP/proveedores/SMTP:

- `previsualizar_comercial`: captura nueva todavía no guardada, ligada a una Quote
  accesible existente para autorización. No crea Quote/revisión/constancia/PDF.
  Valida cabecera, fechas/vigencia, cantidades, unidades, origen y modo comercial,
  FX/identidad y capacidades como creación. No firma precios del browser.
- `previsualizar_edicion_comercial`: reemplazo propuesto de un borrador v2 con
  expected_version y todos los campos de guardar explícitos. Mismo motor y controles
  que guardar; conservar valores ya autorizados no exige permiso para cambiarlos.
  No recaptura originales API bajo el mismo line_key ni modifica capturas/huella/versión.
- `previsualizar_revision_comercial`: lectura consistente de borrador guardado por
  PK y expected_version, bloqueando Quote→revisión. Versión obsoleta o emitida produce
  RevisionConflict. Una emisión se consulta como documento archivado, no preview nuevo.

Devuelven contenedor INTERNO: `persistida`, `edit_version`, `state_fingerprint`,
`resultado` (incluye costos/avisos) y `cliente` (allowlist de salida). El preview de
edición añade `base_state_fingerprint` de la revisión guardada. Su huella propuesta
NO sirve para aceptar cambios aún no guardados: guardar primero, recargar versión
y confirmar avisos de ese estado. Los previews permiten avisos sin confirmarlos.

`cliente` contiene empresa/asesor, cliente, condiciones explícitamente externas,
folio/revisión/fechas, producto/marca/modelo, unidad vendida y cantidad, precio de
VENTA en MXN y sus importes; cabecera con venta antes de descuento, descuento global,
subtotal neto, IVA y total. No contiene proveedor/nombre/ID de distribuidor, originales
de costo/moneda, stock, conversiones/validadores, FX, margen, utilidad ni avisos internos.
No se serializa `resultado` ni se envía una copia filtrada a posteriori del snapshot.
El contexto se construye por allowlist antes del renderer.

Código/nombre/descripción/condiciones son textos externos de producto validados por
el consumidor. El servicio no es un detector/redactor de secretos escritos dentro
de texto libre: no copiar a esos campos datos de proveedor o notas internas. La
identificación/API/código comercial confiable se cerrará en P4. No usar external_id
como fallback automático para el código cliente de v2.

### 9.2 Renderer y precisión visible

Se amplía el único renderer `pdf.py`; la vía histórica v1 conserva su contrato.
V2 muestra Marca/Modelo en vez de Marca/Dist., unidad de venta y desglose global
antes de IVA; no usa fallback de proveedor, stock ni etiqueta FX. Se escapa marcado
de todos los textos igual que v1. No se reescriben originales emitidos.

`commercial_unit_money` elige 2–12 decimales de presentación: aumenta mientras la
multiplicación del unitario mostrado por cantidad no reconcilie a centavos con el
importe, evitando mostrar cero positivo mientras alcance ese límite. El tope12 es técnico,
no nueva precisión de datos ni regla comercial. Si acorta el unitario se etiqueta
`(aprox.)` y se explica que prevalece el importe calculado con precisión completa.
Empates periódicos pueden no reconciliar ni con más decimales; no corregir la partida
para forzar la multiplicación visible. Unitarios largos pasan a detalle sin truncarse.
Importes/totales siguen a dos decimales; descuento no se distribuye entre líneas.
Ajuste P6 (solo PDFs v2 generados después; originales archivados y v1 sin cambios):
la marca `(aprox.)` va en un renglón propio y no cuenta para decidir «Ver detalle»;
marca y modelo en renglones separados con columna más ancha; borrador muestra
«Fecha de propuesta»; empresa sin RFC/teléfono omite esas etiquetas vacías. Las
etiquetas de tasa se normalizan (`16%`, `10%`), sin ceros de escala. Descuento
cero conserva su renglón como `$0.00` (sin signo); solo un descuento mayor lleva `-`.

### 9.3 Emisión y revocación concurrente

`emitir_revision` conserva operación durable propia (§5) y admite v1/v2. En v2:

1. Bloquea Quote→revisión, verifica expected_version, acceso y usuario interno fresco.
2. Si ya está emitida, verifica SHA-256 y devuelve original. No ejecuta motor,
   renderer ni pide volver a aceptar avisos históricos.
3. Para DRAFT, reconstruye estado/cálculo versión1 y exige huella exacta. Valida
   todas las confirmaciones vigentes y sus confirmantes activos/autorizados dentro
   de la misma transacción; una comprobación informativa anterior no sustituye esto.
4. Bloquea en orden por PK las filas User de emisor/confirmantes, y vuelve a consultar
   permisos. El gestor limitado de P2 usa las mismas filas: no puede revocar una
   capacidad entre validación y commit. Cambios de roles por SQL/herramientas técnicas
   fuera de ese gestor siguen sujetos a la frontera de confianza de §2.
5. Renderiza solo contexto cliente, guarda/verifica bytes privados y sella cabecera
   comercial/partidas, sin sobrescribir originales ni asignar `amount` de v1.
6. Marca ISSUED y confirma la transacción. Fallo recuperable limpia solo archivo propio;
   commit incierto conserva archivo para recuperación, igual que v1. SMTP fuera.

Resultado completo: `costs_complete=True`, `gross_profit` firmado; con cualquier
costo ausente conserva costo parcial, `costs_complete=False` y utilidad NULL, incluso
si la venta es conocida. Total0/pérdida/costo ausente se emiten solo tras TODAS las
aceptaciones autorizadas. Reintento no crea constancias, revisión ni PDF nuevos.

### 9.4 Lectura del documento original

`obtener_documento` mantiene selección explícita por número o última emisión. Para
v2 exige usuario interno fresco con acceso a Quote; borrador obtiene contexto cliente
y PDF temporal sin archivar. Emitida devuelve metadatos cliente de importes sellados
y bytes del original verificados por SHA-256, **sin motor ni renderer**.
Se comprueba coherencia de suma de partidas/subtotal/IVA/total sellados sin aplicar
una política comercial nueva. No se deriva un unitario distinto para sustituir el
que aparece en el original: ese contexto de metadatos incluye `archived_only=True`,
no lleva unitarios renderizables y el renderer lo rechaza expresamente.

Descarga/correo existentes pueden reutilizar esos bytes de la misma revisión; las
pruebas usan correo en memoria. Archivo ausente/corrupto requiere restauración exacta,
nunca regeneración. Una revisión nueva no modifica el original v1 ni v2 de su origen.

### 9.5 Alcance operativo

En P3 no se crearon archivos de aplicación nuevos ni otra migración. P4 añade0004
con capturas/recibos técnicos y API comercial (doc28 §13). 0003/0004 siguen pendientes
de autorización para BD de trabajo. No se asignan usuarios/grupos reales ni se
habilita emisión HTTP/mocks. P5 conecta la captura/preview/guardado de interfaz (doc28 §14).
P6 conecta documentos por revisión y guardas de lectura sin otro modelo/migración
(doc28 §15). Falta aceptación comercial final con datos oficiales. Los fixtures y
pruebas sintéticos no certifican semántica real de IVA/stock de Exel ni uso con clientes.
