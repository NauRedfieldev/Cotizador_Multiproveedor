# 31 · M10: punto de extensión de impuestos

## Alcance

`apps/quotes/taxes.py` es la **única fuente de la regla fiscal** del cotizador. Junta
en un solo lugar lo que antes estaba repartido entre `commercial.py`, `models.py`,
`snapshots.py`, `api.py`, la plantilla y `cotizador.js`.

M10 **no cambia ningún importe, pantalla, PDF ni dato guardado**, y no tiene migración.
Las pruebas comparan centavo a centavo contra las fórmulas anteriores
(`apps/quotes/tests/test_taxes.py`).

Fuera de alcance: exenciones, tasas por partida, retenciones y CFDI. Requieren
acuerdo con CCONOR y su área contable (pregunta C10) antes de programarse.

## Regla vigente (C10)

- Una sola tasa de IVA por cotización: **16% inicial**, editable con el permiso
  `change_commercial_tax`.
- El IVA se calcula sobre el **neto** (subtotal de venta menos descuento global) y se
  redondea **una vez** a centavos, HALF_UP.
- Un precio capturado «con IVA» declara su propia tasa y se le quita con esa tasa,
  no con la de la cotización.
- Las tasas son fracciones (`0.16` = 16%), de 0 a 1 con hasta 4 decimales. Siempre
  `Decimal`, nunca `float`.

## Funciones

```python
from apps.quotes.taxes import TASA_IVA_INICIAL, calcular_iva, divisor_iva_incluido, validar_tasa

TASA_IVA_INICIAL                     # Decimal("0.16")
validar_tasa(tasa)                   # tasa exacta o ValidationError (0–1, 4 decimales)
calcular_iva(neto, tasa)             # neto × tasa, HALF_UP a centavos
divisor_iva_incluido(tasa_incluida)  # 1 si None; si no, 1 + tasa
```

`taxes.py` es puro (sin ORM, HTTP ni permisos) y solo importa de `snapshot_values`,
así que `models.py` puede usarlo sin importaciones circulares.

## Quién lo usa

| Lugar | Qué toma de `taxes.py` |
| --- | --- |
| `commercial.py` · `calcular_cotizacion` (v2) | tasa inicial, `validar_tasa`, `calcular_iva`, `divisor_iva_incluido` |
| `snapshots.py` · contexto de documento v1 | `calcular_iva`; validación de tasa en guardar/editar/emitir |
| `models.py` · `QuoteRevision.clean`/`clean_fields` | `validar_tasa`; `DEFAULT_TAX_RATE = TASA_IVA_INICIAL` |
| `api.py` · tasa por defecto de una revisión | `str(TASA_IVA_INICIAL)` |
| `views.py` → `cotizador.html` → `cotizador.js` | la tasa inicial en porcentaje (`"16"`) |

En pantalla, `cotizador.js` lee al iniciar el valor que puso el servidor en
`#taxRate` (`initialTaxPercent`) y lo usa para «Nueva cotización», para la transición
desde un histórico v1 y como tasa sugerida cuando una captura se marca «incluye IVA»
(`initialTaxFraction`). El JS **no calcula IVA**: los totales vienen del servidor.

Excepciones a propósito:
- `Quote.tax` (modelo legado, sin M8) conserva su cálculo: su `tax_rate` no tiene el
  límite 0–1 en la BD, y validarlo podría lanzar errores donde hoy no los hay.
- La lectura de texto HTTP (`_decimal_text` en `api.py`) es formato de entrada, no
  regla fiscal.
- La base de IVA de cada proveedor (`CCONOR_PROVIDER_PRICE_BASES`, `vat_basis` de
  las ofertas) es semántica del proveedor, no de CCONOR (docs/30 A7, B4).

## Inmutabilidad y versiones

- Una cotización **emitida nunca se recalcula**. Su PDF y sus importes son los
  originales (docs/24, docs/25).
- `QuoteRevision.calculation_version` identifica con qué reglas se calculó una
  revisión v2 (`commercial.VERSIONES_CALCULO`; la restricción de la BD
  `quote_revision_commercial_version` acepta esas versiones, migración 0008):
  - `1`: el importe usa el unitario exacto y se redondea una vez (el unitario puede
    mostrar 3+ decimales con «(aprox.)» en el PDF).
  - `2` (vigente desde 2026-11-10, `CALCULO_VIGENTE`): el unitario de venta se redondea
    a centavos (HALF_UP) y el importe es unitario × cantidad. El costo interno no
    cambia de regla. Detalle: docs/28 §14.23.
- Una revisión nueva se crea con la versión vigente. Un borrador pasa a la vigente al
  guardarse (la versión entra en la huella, así que sus confirmaciones de avisos
  vencen); nunca retrocede. Una emitida conserva su versión.

## Cómo agregar una regla fiscal

1. **Acuerdo con CCONOR** (área contable): qué conceptos, qué tasas, cómo se
   presentan base, impuesto y total. Registrarlo en docs/30.
2. **Regla nueva en `taxes.py`**, sin cambiar lo que calcula la actual: la versión
   `1` debe seguir dando los mismos importes.
3. **Nueva `calculation_version`** (la siguiente libre; `2` ya es el redondeo del
   unitario): añadirla a `VERSIONES_CALCULO`, subir `CALCULO_VIGENTE`, migración que
   amplíe la restricción de `QuoteRevision`, y `calcular_cotizacion(version=…)` elige
   la regla según la versión de la revisión.
4. **Datos nuevos** (p. ej. tasa por partida en `QuoteSnapshot`): migración con
   valores nulos para lo existente, sin alterar originales ni huellas guardadas.
5. **Pantalla y PDF**: mostrar el desglose acordado; el PDF de emisiones anteriores
   no cambia.
6. **Pruebas**: equivalencia de la versión `1` (`test_taxes.py`), casos de la regla
   nueva y migración sobre datos existentes.

## Pruebas

```bash
venv/bin/python manage.py test apps.quotes.tests.test_taxes --noinput
venv/bin/python manage.py test apps.quotes.tests.test_vistas_cotizacion.TasaIvaInicialTests --noinput
```

`test_taxes.py` compara `calcular_iva` con un cálculo exacto en fracciones y con las
dos fórmulas anteriores (v2 `_importe`, v1 `amount`), para netos de 0.00 a 20.00 y
varias tasas, incluidos los empates de medio centavo.
