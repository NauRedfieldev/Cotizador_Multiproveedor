# 19 · Propuesta: conversión USD→MXN en `quotes`

| | |
|---|---|
| **Estado** | **Propuesta** aprobada como dirección (alternativa B, 2026-10-05). Se implementará en un plan aparte, con su SDD. Todavía no hay código |
| **Origen** | Brecha I11 de [18 §5](18-pruebas-integracion-syscom-catalog-quotes.md): hoy el precio en USD se cotiza como si fueran pesos |
| **Refina** | D2 de M3 ([16 §12](16-sdd-m3-syscom.md); [15](15-guia-m2-para-m3-syscom.md) D2), que dejó la conversión en manos de `quotes` sin diseñarla. D2 sigue igual: el adaptador pide `moneda=usd` e `iva=0` |
| **Afecta a** | `apps/quotes` (modelos del equipo, M1b). Cambia el formato de los datos guardados, así que hay que acordarlo con el equipo antes de implementarlo |

## 1. El problema

- `ProviderOffer.currency` dice la moneda (SYSCOM: `"USD"`). Pero `Product.unit_price`, `QuoteItem.unit_price` y `Quote` no guardan moneda.
- `Quote.tax = subtotal × tax_rate` aplica el IVA mexicano sobre la cifra en dólares.
- La prueba I11 lo demuestra: 1 × 85.50 USD se cotiza como subtotal 85.50, IVA 13.68 y total 99.18, como si fueran pesos.

## 2. Qué ofrece SYSCOM

Según la documentación pública de developers.syscom.mx, consultada el 2026-10-05 sin llamadas a la API:

- **`GET /productos?moneda=mxn`**: devuelve los precios en pesos. No documenta qué tipo de cambio aplica ni cómo redondea.
- **`GET /api/v1/tipocambio`**: público, sin token. Devuelve un objeto con tipos en texto decimal:
  ```json
  {"normal": "17.50", "preferencial": "17.30", "un_dia": "17.35", "una_semana": "17.40",
   "dos_semanas": "17.45", "tres_semanas": "17.48", "un_mes": "17.55"}
  ```
  Son valores de ejemplo de la documentación, no reales. **El formato real está por confirmar** con una llamada aprobada.
- **`iva=1`** multiplica por 1.16. No se usa, porque `quotes` aplica el IVA una sola vez.

## 3. Alternativas

| | Alternativa | Pros | Contras |
|---|---|---|---|
| **B ✅** | **`quotes` convierte con un tipo de cambio explícito y congelado por cotización.** SYSCOM `/tipocambio` lo sugiere | Una sola regla para todos los proveedores (los que dan USD y los que dan MXN). Auditable: el tipo queda guardado, igual que el snapshot del precio. D2 no cambia y M3 no se toca. Coincide con [17 §6.4](17-adaptador-syscom.md) | Migración en `quotes` (modelo del equipo). Cambia el formato de los datos guardados. Hace falta obtener el tipo de cambio |
| A | Pedir `moneda=mxn` a SYSCOM (cambia D2) | No hay código de conversión, y es lo que SYSCOM factura en pesos | Cada proveedor aplicaría su propio tipo de cambio, así que la comparación mezclaría reglas. No se sabe qué tipo usa SYSCOM. Los proveedores en USD seguirían necesitando conversión. Hay que rehacer pruebas de M3 |
| C | Pedir las dos monedas | Se tienen las dos cifras | Duplica las peticiones, contra un límite sin cifras publicadas |

## 4. Diseño propuesto (B)

### 4.1 Datos (migración en `quotes`)

| Modelo | Campo nuevo | Tipo | Regla |
|---|---|---|---|
| `Quote` | `currency` | `CharField(3)`, por defecto `"MXN"` | Moneda de la cotización (ISO 4217) |
| `Quote` | `exchange_rate` | `DecimalField(10, 4)`, nulo si no hace falta | MXN por 1 USD. Se **congela** al emitir. Es obligatorio si alguna partida viene en otra moneda |
| `Quote` | `exchange_rate_source` | `CharField`, en blanco si no hay | Origen del tipo, p. ej. `"syscom:normal 2026-10-05"` o `"manual"` |
| `QuoteItem` | `source_currency` | `CharField(3)` | Moneda de la oferta o del producto de origen |
| `QuoteItem` | `source_unit_price` | `DecimalField(12, 4)` | Precio en la moneda de origen, sin redondear a 2 decimales |

`Product` tendría que guardar también su moneda (`currency`), porque hoy `unit_price` no la dice. Hay que acordarlo con el equipo, igual que la decisión pendiente sobre qué guarda `Supplier`.

### 4.2 Regla de cálculo

- Si `source_currency == quote.currency`: `unit_price = source_unit_price`.
- Si no: `unit_price = _money(source_unit_price × exchange_rate)`, con `ROUND_HALF_UP` como el resto de `quotes`.
- El IVA se aplica **una sola vez, después de convertir**: `tax = subtotal × tax_rate`. Nada cambia en `subtotal`, `tax` ni `total`.
- El snapshot sigue igual: cambiar el tipo de cambio o el precio después de emitir no altera la cotización.

### 4.3 De dónde sale el tipo de cambio

- **Sugerido:** SYSCOM `GET /api/v1/tipocambio`. Se pide **como mucho una vez al día** y se reutiliza. Es público, pero cuenta para el límite de peticiones.
- **Editable:** el asesor puede cambiarlo antes de emitir. `exchange_rate_source` registra si fue manual.
- **Por decidir en el SDD:**
  - qué campo se usa (`normal` o `preferencial`);
  - dónde vive la función que lo obtiene (en `apps/providers`, con `httpx` y el `base_url` del `Provider` "syscom");
  - cómo se guarda en caché;
  - qué pasa si `/tipocambio` falla (se pide el tipo a mano).

### 4.4 Pruebas que habría que añadir

- La prueba I11 pasa de caracterización a la regla nueva: 85.50 USD × tipo → MXN, y el IVA sobre la cifra en pesos.
- Moneda igual sin conversión; moneda distinta sin tipo da error de validación; redondeo half-up de la conversión.
- El tipo queda congelado tras emitir; `/tipocambio` caído permite el tipo manual; la respuesta de `/tipocambio` se lee con `parse_float=Decimal`.
- Antes: 1 llamada real aprobada a `/tipocambio` para confirmar el formato y guardar una muestra saneada en `apps/providers/tests/muestras/`.

## 5. Siguiente paso

1. Acordar con el equipo el cambio en `quotes` y en `Product`.
2. Escribir el SDD (con la numeración que toque) que resuelva lo pendiente de §4.3.
3. Pedir el presupuesto de la llamada real a `/tipocambio`.
