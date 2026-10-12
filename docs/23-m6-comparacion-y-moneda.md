# M6: comparación de ofertas y moneda

## Alcance

`apps/quotes/comparison.py` compara los `ProviderResult` de M2 y devuelve las
mejores ofertas **por proveedor**, expresadas en MXN. Es un componente de solo
lectura: no consulta APIs, no usa ORM ni escribe en BD o modifica cotizaciones.

No se cambian `ProviderOffer`, `ProviderResult`, `Product`, `QuoteItem`, `Quote`,
los adaptadores, M5 ni el PDF. No hay migraciones ni dependencias nuevas.

**El ranking es de precios, no de disponibilidad ni equivalencia de productos.**
El consumidor delimita la búsqueda y valida qué productos son alternativas.
M6 no convierte una sugerencia fuzzy pendiente de revisión en identidad confirmada.

## Operaciones

```python
comparar(resultados, *, tipo_cambio=None, n=3)
comparar_configurado(resultados, *, tipo_cambio=None, n=None)
tipo_cambio_manual(texto, *, fecha)
```

- `comparar`: núcleo puro, sin Django settings. Recibe un iterable de
  `ProviderResult` materializados; no pasar QuerySets o iterables que consulten BD.
- `comparar_configurado`: entrada Django; cuando `n is None`, lee
  `settings.OFERTAS_POR_PROVEEDOR`, configurado por `.env`, con predeterminado 3.
  Un `n` explícito prevalece. No ejecuta ORM ni llamadas de red.
- `tipo_cambio_manual`: valida una captura de texto y devuelve el contexto de FX.
  No es un formulario ni endpoint.

`n` debe ser entero positivo; booleanos, texto, decimales y valores ≤0 se rechazan.
El límite es estricto, incluso si hay empates. La variable se documenta también
en `.env_example`; no hay un tipo de cambio predeterminado en configuración.

## Disponibilidad: política acordada

| `ProviderOffer.stock` | Tratamiento |
| --- | --- |
| Mayor que cero | Incluido; existencia reportada por proveedor |
| `0` | Incluido; sin existencias |
| `None` | Incluido; disponibilidad desconocida, no equivale a cero |

Se conserva el valor original. No se ordena primero por stock ni se asigna una
existencia ficticia. **La oferta de mejor precio puede tener stock cero o desconocido.**
La futura UI debe mostrar esta diferencia y no prometer disponibilidad.

## Tipo de cambio explícito y manual

`TipoCambio` es una dataclass inmutable con:

| Campo | Regla |
| --- | --- |
| `valor` | `Decimal` positivo y finito: MXN por 1 USD |
| `origen` | Texto no vacío; `"manual"` o identificación de una fuente externa |
| `fecha` | `datetime` de captura con zona horaria, aportado por el consumidor |

No se inventa fecha, origen ni tasa. No se usa `float`. M6 aplica el mismo contexto
a todas las ofertas USD de una comparación; no decide qué tasa utilizar por proveedor.

| Moneda de oferta | Precio comparable |
| --- | --- |
| `MXN` | El `Decimal` original, sin conversión |
| `USD` | Precio original × `tipo_cambio.valor`, sin cuantizar |
| Otra | Error explícito de validación; no se devuelve ranking parcial |

Una oferta USD requiere tipo de cambio aunque su precio sea cero o quedaría fuera
del top `n`. Nunca se comparan dólares como si fueran pesos ni se asume tasa 1.
Si solo hay ofertas MXN, el contexto puede ser `None`.

### Captura manual futura

La vista debe aportar el texto capturado y la fecha de captura. El helper admite
enteros o decimales con punto y espacios exteriores: `"18"`, `" 17.123456 "`.
Rechaza comas, separadores de miles, notación científica, formatos incompletos,
NaN, infinito y valores no positivos. No interpreta silenciosamente `"17,50"`.

```python
tipo = tipo_cambio_manual("17.50", fecha=instante_captura)
```

La construcción directa de `TipoCambio` permite una fuente externa previamente
validada. Obtener `/tipocambio`, la caché y el campo de captura en pantalla son
integraciones posteriores: este módulo no hace llamadas ni conecta formularios.

## Precisión: regla crítica

**No redondear el precio unitario a dos decimales antes de convertir u ordenar.**

1. `oferta.price` permanece intacto, incluidos seis o más decimales de origen.
2. La conversión multiplica coeficientes con precisión suficiente en un contexto
   decimal local propio; no depende de la precisión, límites o traps del llamador.
3. El contexto global no se modifica. El producto no se cuantiza ni se trunca.
4. Se ordena usando el valor comparable completo, no su representación visual.
5. Los extremos que no pueden representarse exactamente generan error, no un
   resultado redondeado, infinito o cero silencioso.

Ejemplos sintéticos:

```text
382.533200 USD × 17.123456 = 6550.290418739200 MXN
1.004001 MXN < 1.004999 MXN, aunque ambos se mostrarían como 1.00
```

El comparable no reemplaza el precio original: ambos permanecen en la salida.
No es un precio final con IVA, flete, descuento adicional o margen de venta.
El adaptador y el equipo deben verificar que los precios comparados tengan una
base comercial compatible; convertir moneda no corrige un IVA incluido en uno
de los proveedores y excluido en otro.

### Presentación y futura totalización

La política aprobada para la integración posterior es:

```text
importe_partida = ROUND_HALF_UP(precio_origen × factor_moneda × cantidad, 0.01)
subtotal = suma de importes de partidas ya redondeados
```

El factor es 1 para MXN y el tipo de cambio para USD. No se redondea antes de
multiplicar por cantidad: `1.004 × 10 = 10.04`, no `1.00 × 10 = 10.00`.

Los precios unitarios deben mostrar los decimales necesarios para que el asesor
pueda entender el importe; importes y totales se presentan a dos decimales.
Un empate se decide por precio completo, no por coincidir a centavos.

**M6 no implementa la totalización ni cambia los modelos actuales.** `Product` y
`QuoteItem` siguen almacenando precios con dos decimales; guardar allí el valor
completo perdería precisión. `Quote` tampoco guarda aún moneda o tipo de cambio.
La migración y el snapshot deben acordarse antes de integrar esta salida con la
persistencia. No asumir que los precios históricos eran todos MXN.

## Resultado y empates

`ResultadoComparacion` tiene los siguientes campos:

| Campo | Contenido |
| --- | --- |
| `por_proveedor` | Mapa de código → tupla de `OfertaComparada`, ordenada por precio e ID externo |
| `errores` | Mapa de código → `ProviderError` original de M2 |
| `proveedores_con_empate` | Conjunto inmutable de códigos con varios candidatos al mejor precio, antes de limitar |
| `tipo_cambio` | Contexto original recibido, o `None` |
| `moneda` | `"MXN"`, moneda de los precios comparables, no de las ofertas originales |

Cada `OfertaComparada` contiene `oferta` original, `precio_comparable` y
`es_mejor_precio`. Se marcan todas las ofertas devueltas que compartan el mínimo.
Si `n=1` limita un empate, la salida aún lo señala en `proveedores_con_empate`.
Para empates exactos, `external_id` define un orden textual estable, no una
preferencia comercial. Los mapas se ordenan por `provider_code`, no por posición.

Las dataclasses y contenedores de salida no son editables. No se clona ni inspecciona
`raw`; la oferta y excepción originales siguen referenciadas. Esto no constituye
un snapshot persistido ni congela profundamente el payload o una excepción mutable.

Un proveedor válido sin ofertas aparece con tupla vacía. Un proveedor fallido
aparece en `errores`, sin usar sus ofertas, y no bloquea a los válidos.

Errores M2 y errores del comparador son distintos: el primero se conserva como
dato; el segundo lanza `TypeError`/`ValueError` sin devolver una comparación parcial.
Se rechazan códigos vacíos, tipos incompatibles, ofertas/errores de otro proveedor,
resultados duplicados de un proveedor e IDs externos duplicados dentro de él.
No se sobrescribe ni selecciona arbitrariamente información incoherente.
El mismo ID externo sí puede existir en dos proveedores distintos.

## Ejemplo sintético ejecutable

```python
from datetime import datetime, timezone
from decimal import Decimal
from apps.providers.contracts import ProviderOffer, ProviderResult
from apps.quotes.comparison import comparar, tipo_cambio_manual

tipo = tipo_cambio_manual("17.50", fecha=datetime.now(timezone.utc))
ofertas = (
    ProviderOffer("nuevo", "usd", "Equipo sintético", Decimal("10"), "USD", stock=0),
    ProviderOffer("nuevo", "mxn", "Equipo sintético", Decimal("180"), "MXN", stock=None),
)
salida = comparar([ProviderResult("nuevo", offers=ofertas)], tipo_cambio=tipo, n=3)
# USD: comparable 175.00 MXN, mejor precio, stock 0.
# MXN: comparable 180 MXN, stock desconocido.
```

Usar `comparar_configurado` para aplicar el límite del entorno en Django.
Desde ASGI, el núcleo puede invocarse con datos materializados: no hace I/O.
Las consultas previas y la futura persistencia requieren sus propias vías async.

## Nuevos proveedores

M6 no tiene ramas por SYSCOM/Exel ni lee sus payloads. Un nuevo adaptador que
produzca los contratos M2 con precios comparables en USD/MXN no requiere cambiar
el ranking. Otras monedas o condiciones comerciales necesitan una ampliación
explícita, no una conversión genérica supuesta. Consultar la
[guía de integración](20-guia-integrar-api-de-proveedor.md).

## Pruebas y trabajo posterior

```bash
venv/bin/python manage.py test apps.quotes.tests.test_comparison --noinput
venv/bin/python manage.py test --noinput
venv/bin/python -m unittest discover -s tools -p 'test_*.py' -v
venv/bin/python manage.py check
venv/bin/python manage.py makemigrations --check --dry-run
venv/bin/python manage.py migrate --check
git diff --check
```

Las pruebas de M6 usan proveedores ficticios y `SimpleTestCase`, que prohíbe ORM.
Cubren monedas, captura manual, precisión, contexto decimal ambiente, stock,
empates, límites/configuración, fallos, duplicados, inmutabilidad y ausencia de red.
La regresión completa se valida con PostgreSQL, sin llamadas reales a proveedores.

La prueba I11 de caracterización sigue pasando: el flujo de referencia que guarda
USD como pesos aún no utiliza M6. **Publicar este módulo no corrige por sí solo
la creación de cotizaciones ni la precisión de los campos almacenados.**

Después: integración M7, obtención/caché de FX, migraciones acordadas y congelación
de precio/moneda/tipo en snapshot M8. PDF e impuestos quedan separados.
