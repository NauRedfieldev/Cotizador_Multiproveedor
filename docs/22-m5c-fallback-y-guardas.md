# M5c: sugerencias por similitud y guardas técnicas

## Alcance

M5c complementa el [matching determinístico M5b](21-catalogo-canonico-y-m5b.md)
cuando este devuelve `SIN_MATCH`. Usa RapidFuzz ya incluido en las dependencias;
no añade paquetes, modifica modelos ni requiere migraciones.

**El fuzzy matching solo sugiere candidatos para revisión. Nunca devuelve
`AUTO_MATCH`, ni siquiera con score 100.** No persiste asociaciones, crea canónicos,
modifica cotizaciones, consulta proveedores ni conecta pantallas. `Product`,
`QuoteItem`, `ProviderOffer` y los adaptadores permanecen intactos.

La marca y categoría restringen la búsqueda; las guardas rechazan incompatibilidades
explícitas. Esto no demuestra equivalencia de productos: una descripción incompleta
o una diferencia no cubierta por las guardas puede recibir una puntuación alta.

## Operaciones y entradas

Las operaciones existentes `resolver_match`, `emparejar` y `aemparejar` siguen siendo
exclusivamente M5b. El consumidor opta explícitamente por las nuevas operaciones
de `apps.catalog.matching`:

```python
resolver_con_fallback(
    oferta, candidatos, categoria_id=..., sinonimos=..., confianza=...,
)
emparejar_con_fallback(oferta, categoria_id=..., confianza=...)
await aemparejar_con_fallback(oferta, categoria_id=..., confianza=...)
```

- `oferta`: `ProviderOffer`; se usa `name` como descripción y `brand` como marca.
- `categoria_id`: ID entero positivo de una categoría **interna**. Es obligatorio
  como argumento; `None` impide el fallback, pero no impide una decisión de M5b.
  No se infiere desde códigos externos, el nombre ni `raw`.
- `confianza`: `IdentificadoresConfiables`, sin identificadores autorizados por
  defecto. La procedencia debe verificarse fuera del matcher, como en M5b.
- `candidatos`: solo en la operación en memoria; lista/iterable de canónicos
  materializados, con ID y campos cargados. No pasar un QuerySet o campos diferidos
  desde un event loop: materializarlos fuera de él. Para no ocultar ambigüedades,
  incluir todos los candidatos relevantes de M5b, incluso los de otras categorías.
- `sinonimos`: solo en memoria; instantánea normalizada del diccionario aprobado,
  obtenida con `obtener_sinonimos()` fuera del event loop, o fixture de prueba.

La operación con BD verifica que la categoría exista y esté activa. La operación
en memoria solo verifica el tipo/valor del ID: el consumidor debe validar su
existencia y estado. Ambas exigen marca y texto con información para el fallback.

No se lee `raw`, `external_id`, `provider_code`, precio, moneda ni stock para decidir
identidad; no se infieren identificadores desde ellos. El SKU de Exel no se declara
SKU de fabricante hasta confirmar su significado.

## Flujo

```text
M5b (sin restringir por categoría)
 ├─ AUTO_MATCH       → conservar íntegramente el resultado
 ├─ REVISION_MANUAL  → conservar íntegramente el resultado
 └─ SIN_MATCH
      → validar marca, categoría y descripción
      → candidatos de la misma marca normalizada y categoría
      → descartar colisiones de originales de marca e identificadores confiables
      → normalizar descripciones y expandir sinónimos
      → puntuar TODOS los candidatos con token_set_ratio
      → descartar incompatibilidades técnicas
      → sugerir todos los compatibles que alcancen 70
```

Los originales de marca e identificadores solo ignoran mayúsculas/minúsculas y
espacios exteriores, igual que M5b. Un identificador confiable distinto en el
canónico descarta ese candidato; uno ausente no prueba incompatibilidad. Los
identificadores no confirmados no autorizan coincidencias ni bloquean sugerencias.

M5c **nunca anula** duplicados, colisiones o contradicciones enviados a revisión
por M5b. Los filtros de categoría solo se aplican después de esa decisión.

La expansión usa la misma instantánea de sinónimos para la oferta y todos los
candidatos. Se aplican tokens completos, sin expansión recursiva. No se guardan
descripciones expandidas: los cambios del diccionario no requieren reescribir
canónicos. Sigue vigente la caché por proceso de 60 s y su invalidación existente.

## Guardas iniciales de redes

`apps.catalog.specs.specs()` recibe texto normalizado **sin expandir sinónimos** y
devuelve conjuntos inmutables de valores explícitos. `compatibles()` compara esos
conjuntos; un sinónimo no puede borrar una contradicción de `POE+` o licencia.

| Atributo | Formas reconocidas, ejemplos |
| --- | --- |
| Puertos | `24P`, `24 P`, `24 PUERTO`, `24 PUERTOS` |
| Uplinks | `4X1G`, `4 X 10G`; cantidad y velocidad forman una pareja |
| PoE | `POE`, `POE+`, `POE++`, `SIN POE`, `NO POE` |
| Licencia | `LAN BASE`, `LAN LITE`, `IP BASE`, `IP SERVICES`, también sin espacio |
| Rack | `1U`, `2 U`, `1.5U` |

Reglas:

- Valores distintos en ambos textos rechazan el candidato.
- Si un atributo solo aparece en uno, no se asume igualdad ni contradicción.
- Varias apariciones iguales no crean ambigüedad.
- Varios valores distintos de puertos, PoE, licencia o rack se tratan como
  información ambigua y rechazan el texto, aunque falte el atributo en el otro.
  Puede reducir sugerencias para equipos que describen configuraciones mixtas.
- Pueden coexistir varios uplinks; si ambos textos los declaran, se compara el
  conjunto completo, sin escoger solo la primera aparición.
- Los patrones evitan fragmentos alfanuméricos como `ABC24P` o `MODELO4X10G`.
  Son extractores acotados, no un analizador completo de especificaciones.

No se convierten unidades ni se interpretan todos los formatos: `24 PORTS`,
velocidades decimales como `2.5G`, otras licencias y negaciones no enumeradas no
están cubiertos. Tampoco se validan capacidad de discos, resolución de cámaras,
consumo PoE, compatibilidad de accesorios ni variantes de otras categorías.
Los productos de estas categorías pueden recibir sugerencias, siempre manuales.
Ampliar cobertura y habilitar automatización exige reglas y pruebas adicionales.

## Resultado

Se reutiliza `ResultadoMatch`, sin cambiar su estructura. Se añade `MetodoMatch.FUZZY`
a las opciones existentes; no se modifica ningún formato almacenado.

- `canonico`: siempre `None` para M5c; nunca se elige una identidad.
- `estado`: `REVISION_MANUAL` si hay sugerencias; `SIN_MATCH` en caso contrario.
- `metodo`: `FUZZY`, incluso si el contexto impide ejecutar la puntuación.
- `score`: mejor puntuación entre los candidatos compatibles, truncada a entero;
  `None` si no se puntuó ningún candidato compatible. No es una probabilidad.
- `candidatos`: IDs únicos de **todos** los candidatos compatibles con score ≥70,
  ordenados por ID, **no por relevancia**. No son únicamente los empatados ni se
  devuelve puntuación individual de cada candidato.
- `motivo`: explica la decisión según la tabla siguiente.

Los umbrales se comparan usando el score original de RapidFuzz, sin redondear hacia
arriba: 69.999 no llega a 70 y 89.999 no llega a 90. Un empate compara puntuaciones
originales, no las truncadas. Puntuaciones cercanas siguen siendo sugerencias;
no se inventa un margen de selección automática.

| Motivo | Estado / interpretación |
| --- | --- |
| `FALLBACK_SIN_MARCA` | SIN_MATCH; falta marca normalizable |
| `FALLBACK_SIN_CATEGORIA` | SIN_MATCH; se recibió `None` |
| `FALLBACK_CATEGORIA_INVALIDA` | SIN_MATCH; ID inválido o categoría inexistente/inactiva en BD |
| `FALLBACK_TEXTO_SIN_INFORMACION` | SIN_MATCH; descripción sin letras/números |
| `FALLBACK_SIN_CANDIDATOS` | SIN_MATCH; bloqueo por marca/categoría sin candidatos |
| `FALLBACK_SIN_CANDIDATOS_COMPATIBLES` | SIN_MATCH; todos descartados por originales, identificadores, descripción o guardas |
| `FUZZY_BAJO_UMBRAL` | SIN_MATCH; mejor candidato compatible menor a 70 |
| `SUGERENCIAS_FUZZY` | REVISION_MANUAL; mejor score entre 70 y menos de 90 |
| `FUZZY_UMBRAL_ALTO_SIN_CALIBRAR` | REVISION_MANUAL; mejor score ≥90, sin empate en el primer lugar |
| `FUZZY_EMPATE` | REVISION_MANUAL; varios candidatos con el mejor score ≥70 |

Si M5b obtiene una decisión, sus campos y motivos permanecen intactos; puede
devolver `AUTO_MATCH` determinístico. `UMBRAL_AUTO_MATCH=90` es aquí solo una señal
provisional, **no habilita auto-match fuzzy**.

## Ejemplo de consumo con BD (sintético)

Con canónicos y sinónimos previamente curados en el admin:

```python
from decimal import Decimal
from apps.catalog.matching import IdentificadoresConfiables, emparejar_con_fallback
from apps.providers.contracts import ProviderOffer

oferta = ProviderOffer(
    provider_code="proveedor_sintetico", external_id="123",
    name="SW 24P POE+ 4X1G LAN BASE 1U", brand="Marca X",
    price=Decimal("382.533200"), currency="MXN",
)
resultado = emparejar_con_fallback(
    oferta,
    categoria_id=categoria_interna.pk,  # resuelta y validada por el consumidor
    confianza=IdentificadoresConfiables(),
)
```

Si existe `SWITCH 24P POE+ 4X1G LAN BASE 1U` de la misma marca y categoría, y el
diccionario incluye `SW → SWITCH`, obtendrá score 100 y revisión manual. Una variante
`48P`, `POE`, `4X10G`, `LAN LITE` o `2U` se descarta aunque tenga score textual alto.

Desde una vista ASGI usar `await aemparejar_con_fallback(...)`. El ORM, la carga de
sinónimos y la puntuación se ejecutan fuera del event loop mediante
`sync_to_async(thread_sensitive=True)`. No se añaden escrituras ni llamadas de red.

## Pruebas y cierre

Desde la raíz, con el entorno y PostgreSQL configurados:

```bash
venv/bin/python manage.py test apps.catalog.tests.test_specs apps.catalog.tests.test_fallback --noinput
venv/bin/python manage.py test --noinput
venv/bin/python manage.py check
venv/bin/python manage.py makemigrations --check --dry-run
venv/bin/python manage.py migrate --check
venv/bin/python -m unittest discover -s tools -p 'test_*.py' -v
git diff --check
```

Las pruebas son sintéticas y usan la BD temporal de pruebas. Cubren guardas,
sinónimos, blocking, límites sin redondeo, empates, identificadores contradictorios,
prioridad de M5b, solo SELECT, ausencia de red y ejecución async. No cargan datos
en el catálogo de desarrollo ni acreditan precisión comercial con proveedores reales.

Antes de automatizar: reunir pares reales etiquetados de SYSCOM/Exel, incluyendo
variantes difíciles; medir precisión, recall y falsos positivos por categoría;
calibrar umbrales y aprobar cobertura técnica. Una muestra inicial de 50–100 pares
ayuda a explorar, pero no garantiza que la automatización sea segura.

Persistencia/auditoría, cola de revisión, enlace con `Product`, integración UI,
clasificación de ofertas y correcciones de adaptadores quedan como tareas separadas.
