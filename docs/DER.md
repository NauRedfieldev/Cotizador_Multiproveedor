# DER — Catálogo y Cotizaciones (M1b + revisiones M8)

Diagrama Entidad-Relación de las apps `catalog` y `quotes`.
Fuente de verdad: `apps/catalog/models.py` y `apps/quotes/models.py`.

## Diagrama (Mermaid)

```mermaid
erDiagram
    AUTH_USER ||--o{ QUOTE : "asesora (1:N)"
    CATEGORY ||--o{ PRODUCT : "clasifica (1:N)"
    SUPPLIER ||--o{ PRODUCT : "distribuye (1:N)"
    PROVIDER |o--o| SUPPLIER : "se consulta por API (0..1)"
    QUOTE ||--o{ QUOTE_ITEM : "contiene (1:N)"
    PRODUCT ||--o{ QUOTE_ITEM : "se cotiza en (0:N)"
    QUOTE ||--o{ QUOTE_REVISION : "versiona (PROTECT)"
    QUOTE_REVISION |o--o{ QUOTE_REVISION : "origina (based_on, PROTECT)"
    QUOTE_REVISION ||--o{ QUOTE_SNAPSHOT : "captura (PROTECT)"
    QUOTE_REVISION ||--o{ QUOTE_COMMERCIAL_CONFIRMATION : "confirma estado (PROTECT)"
    AUTH_USER ||--o{ QUOTE_COMMERCIAL_CONFIRMATION : "acepta aviso (PROTECT)"
    AUTH_USER |o--o{ QUOTE_SNAPSHOT : "valida unidad (PROTECT)"

    AUTH_USER {
        int id PK
        varchar username
    }

    CATEGORY {
        int id PK
        varchar name UK
        text description
        bool is_active
        datetime created_at
        datetime updated_at
    }

    SUPPLIER {
        int id PK
        varchar name UK "ej. SYSCOM, Exel del Norte"
        varchar code UK "nullable"
        bigint provider_id FK "UK, nullable, PROTECT → providers_provider"
        varchar email
        varchar phone
        varchar website
        bool is_active
        datetime created_at
        datetime updated_at
    }

    PRODUCT {
        int id PK
        varchar sku UK "ej. PRO-CAT6-EXT"
        varchar name
        text description
        int category_id FK "PROTECT"
        int supplier_id FK "PROTECT"
        decimal unit_price "precio base vigente"
        bool is_active
        datetime created_at
        datetime updated_at
    }

    QUOTE {
        int id PK
        varchar folio UK "COT-YYYY-XXXX, autogenerado"
        varchar client_name "razón social"
        varchar client_rfc "opcional, validado"
        varchar client_email
        int advisor_id FK "auth_user, PROTECT"
        date issue_date "default hoy"
        smallint valid_days "default 15"
        varchar status "DRAFT|ISSUED|SENT|ACCEPTED"
        decimal tax_rate "default 0.16"
        text notes
        datetime created_at
        datetime updated_at
    }

    QUOTE_ITEM {
        int id PK
        int quote_id FK "CASCADE"
        int product_id FK "nullable, SET_NULL"
        text description "snapshot del producto"
        decimal quantity "> 0"
        decimal unit_price "snapshot del producto"
    }

    QUOTE_REVISION {
        bigint id PK
        bigint quote_id FK "PROTECT"
        bigint based_on_id FK "nullable, PROTECT"
        int number "UK con quote_id"
        uuid request_id "UK con quote_id"
        varchar request_fingerprint "SHA-256 solicitud inicial"
        int edit_version "control optimista"
        varchar status "DRAFT o ISSUED"
        smallint schema_version "1 histórico o 2 comercial"
        smallint calculation_version "nullable v1, 1 en v2"
        varchar state_fingerprint "estado actual v2"
        uuid exchange_context_id "nullable; identidad FX v2"
        decimal discount_percent "0 a 100, solo v2"
        decimal sale_subtotal "venta antes descuento, solo sellado v2"
        decimal discount_amount "solo sellado v2"
        decimal known_cost_total "parcial si faltan costos"
        bool costs_complete "nullable hasta sellado v2"
        decimal gross_profit "firmado o NULL si incompleto"
        jsonb header "folio, empresa, asesor, cliente, condiciones"
        varchar currency "MXN"
        decimal exchange_rate "numeric(18,8), nullable"
        varchar exchange_rate_source
        datetime exchange_rate_date "aware, nullable"
        decimal tax_rate "numeric(5,4)"
        date issue_date
        smallint valid_days
        decimal subtotal "numeric(38,2), nullable en borrador"
        decimal tax "numeric(38,2), nullable en borrador"
        decimal total "numeric(38,2), nullable en borrador"
        varchar pdf "ruta privada del original"
        varchar pdf_sha256
        datetime issued_at "nullable"
        datetime created_at
        datetime updated_at
    }

    QUOTE_SNAPSHOT {
        bigint id PK
        bigint revision_id FK "PROTECT"
        int position "UK con revision_id"
        text code
        text name
        text description
        varchar provider_code
        varchar provider_name
        text external_id
        text brand
        text model
        text mpn
        decimal source_unit_price "numeric(18,6); NULL solo costo manual desconocido v2"
        varchar source_currency "USD o MXN; nullable con costo desconocido v2"
        decimal quantity "numeric(10,2)"
        bigint stock "nullable; cero distinto de NULL"
        bool es_mejor_precio
        decimal amount "numeric(38,2), reservado v1"
        datetime captured_at
        uuid line_key "UK con revision, NULL v1"
        varchar source_kind "API o MANUAL, NULL v1"
        decimal cost_tax_rate_included "NULL neto o tasa incluida"
        varchar source_unit_label
        varchar sale_unit_label
        decimal conversion_factor "divisor positivo, NULL v1"
        bigint unit_validated_by_id FK "nullable v1, PROTECT"
        datetime unit_validated_at
        varchar pricing_mode "MARGIN o MANUAL, NULL v1"
        decimal margin_percent "0 a 99.99 en modo margen"
        decimal manual_unit_price "capturado por unidad vendida"
        varchar manual_currency
        decimal manual_tax_rate_included
        decimal sale_amount "antes descuento, solo sellado v2"
        decimal cost_amount "solo sellado v2 si costo conocido"
    }

    QUOTE_COMMERCIAL_CONFIRMATION {
        bigint id PK
        bigint revision_id FK "PROTECT"
        int edit_version
        varchar state_fingerprint
        varchar warning_code
        varchar scope_ref "UUID lógico de partida o vacío global"
        bigint confirmed_by_id FK "PROTECT"
        datetime confirmed_at
    }
```

> Las propiedades calculadas (`Quote.subtotal`, `Quote.tax`, `Quote.total`,
> `Quote.expiration_date`, `QuoteItem.amount`) **no son columnas** de la BD;
> se derivan en Python/SQL al consultarse.

## Diccionario de datos

### catalog_category — Clasificación de productos

| Campo | Tipo | Clave | Descripción |
|---|---|---|---|
| id | serial | PK | Identificador interno |
| name | varchar(100) | UK | Nombre de la categoría (ej. Cableado estructurado) |
| description | text | | Descripción opcional |
| is_active | bool | | Permite desactivar sin borrar |
| created_at / updated_at | timestamptz | | Auditoría automática |

### catalog_supplier — Marca o distribuidor

| Campo | Tipo | Clave | Descripción |
|---|---|---|---|
| id | serial | PK | Identificador interno |
| name | varchar(100) | UK | Nombre comercial (ej. SYSCOM, Exel del Norte) |
| code | varchar(20) | UK (nullable) | Clave/abreviatura interna |
| provider_id | bigint | FK → providers_provider, UK (nullable) | Configuración de su API en `apps.providers`. Solo para distribuidores que se consultan por API. PROTECT: no se borra un `Provider` enlazado. Ver duda 7 del SDD de M2 (alternativa A) |
| email / phone / website | varchar | | Datos de contacto opcionales |
| is_active | bool | | Permite desactivar sin borrar |
| created_at / updated_at | timestamptz | | Auditoría automática |

### catalog_product — Producto cotizable

| Campo | Tipo | Clave | Descripción |
|---|---|---|---|
| id | serial | PK | Identificador interno |
| sku | varchar(50) | UK | Código/clave único (ej. PRO-CAT6-EXT) |
| name | varchar(200) | | Nombre corto |
| description | text | | Descripción detallada para la cotización |
| category_id | int | FK → category | PROTECT: no se borra una categoría con productos |
| supplier_id | int | FK → supplier | PROTECT: idem para proveedor |
| unit_price | numeric(12,2) | | Precio unitario base vigente (≥ 0) |
| is_active | bool | | Permite desactivar sin borrar |
| created_at / updated_at | timestamptz | | Auditoría automática |

### quotes_quote — Cabecera de cotización

| Campo | Tipo | Clave | Descripción |
|---|---|---|---|
| id | serial | PK | Identificador interno |
| folio | varchar(20) | UK | Folio autogenerado COT-YYYY-XXXX (correlativo anual) |
| client_name | varchar(255) | | Razón social del cliente |
| client_rfc | varchar(13) | | RFC opcional, validado con regex oficial |
| client_email | varchar | | Correo del cliente (para envío) |
| advisor_id | int | FK → auth_user | Asesor técnico a cargo; PROTECT para preservar historial |
| issue_date | date | | Fecha de emisión (default: hoy) |
| valid_days | smallint | | Días de vigencia (default: 15) |
| status | varchar(10) | | DRAFT → ISSUED → SENT → ACCEPTED |
| tax_rate | numeric(5,4) | | Tasa de IVA de la cotización (default: 0.16) |
| notes | text | | Notas / condiciones comerciales |
| created_at / updated_at | timestamptz | | Auditoría automática |

### quotes_quoteitem — Partida de cotización

| Campo | Tipo | Clave | Descripción |
|---|---|---|---|
| id | serial | PK | Identificador interno |
| quote_id | int | FK → quote | CASCADE: se borra con su cotización |
| product_id | int | FK → product | Nullable; SET_NULL: la partida sobrevive si el producto se borra |
| description | text | | Snapshot de la descripción al cotizar (editable) |
| quantity | numeric(10,2) | | Cantidad (> 0; decimal para metros/piezas fraccionadas) |
| unit_price | numeric(12,2) | | Precio unitario congelado al momento de cotizar |

### quotes_quoterevision / quotes_quotesnapshot / quotes_quotecommercialconfirmation — Historial M8

Diccionario completo, restricciones y capacidad financiera:
[24 · SDD M8 §2–3](24-sdd-m8-snapshot-pdf.md). Operaciones/permisos:
[25 · Integración M8](25-m8-snapshot-pdf-integracion.md). Las tablas existentes
permanecen intactas, sin inferir moneda/FX histórica. Los nuevos documentos usan
snapshots explícitos, no unitarios de dos decimales de QuoteItem.

V2/P2: campos/constraints/confirmaciones en [24 §8](24-sdd-m8-snapshot-pdf.md#8-ampliación-comercial-v2--p1p2)
y servicios/roles en [25 §7](25-m8-snapshot-pdf-integracion.md#7-servicios-comerciales-v2-p2).
0003 añade estructura sin convertir v1; source_unit_price/currency admiten NULL solo
en costo manual desconocido v2. Cantidad entera para v2 sin eliminar decimales v1.
Constancia única por revisión/versión/huella/código/alcance/actor, inmutable. No hay
descuento por partida ni duplicación del usuario/roles: se reutiliza auth.Group/Permission.
P2 todavía no emite documento comercial v2 ni cambia endpoints/UI de M7a.

## Notas de diseño

- **Snapshot parcial legacy en partidas**: al guardar un `QuoteItem` ligado a un producto, si
  `description` o `unit_price` vienen vacíos se copian del catálogo. Así, un
  cambio de precio en Product no cambia el precio copiado; nombres/proveedor y
  datos de Quote siguen siendo mutables. No garantiza historial emitido completo.
- **Totales legacy calculados, no persistidos**: `subtotal` se calcula con agregación
  SQL (`SUM(quantity × unit_price)`), `tax = subtotal × tax_rate` y
  `total = subtotal + tax`. Se evita desnormalización e inconsistencias.
- **Revisiones M8**: borradores capturan cabecera/partidas/FX; emisión persiste
  importes redondeados por línea y totales, PDF privado y SHA-256. Emisiones
  inmutables en interfaz de escritura soportada; cambios requieren nueva revisión.
  No usar los totales legacy para documentos M8.
- **Folio**: se genera en `Quote.save()` con formato `COT-YYYY-XXXX`
  (consecutivo que reinicia cada año). La unicidad la garantiza el constraint
  `unique`; ante colisión por concurrencia se reintenta el guardado.
- **Vigencia**: `expiration_date = issue_date + valid_days` (derivada) e
  `is_expired` para validar cotizaciones vencidas.
- **Borrado seguro**: `PROTECT` en `advisor`, `category` y `supplier`
  preserva la integridad del historial comercial.
- **Supplier ↔ Provider** (2026-10-05, alternativa A de la duda 7 del SDD de M2):
  `Supplier.provider` enlaza, de forma opcional y uno a uno, el distribuidor con la
  configuración de su API en `apps.providers`. Las ofertas que devuelve `search_all`
  traen `provider_code`, y su Supplier se obtiene con
  `Supplier.objects.aget(provider__code=oferta.provider_code)`. La dependencia va de
  `catalog` a `providers`, nunca al revés.
- **Tablas físicas** (convención Django `app_modelo`): `catalog_category`,
  `catalog_supplier`, `catalog_product`, `quotes_quote`, `quotes_quoteitem`,
   `quotes_quoterevision`, `quotes_quotesnapshot`, `quotes_quotecommercialconfirmation`.
