# Catálogo canónico y matching determinístico M5b

## Alcance y compatibilidad

`ProductoCanonico` representa una identidad de producto independiente de sus ofertas.
Conserva marca, identificadores originales, claves derivadas, descripción y categoría.
No guarda proveedor, precio, moneda ni existencia. Una variante técnica es otra identidad.

Esta entrega no modifica `Product`, `QuoteItem`, `ProviderOffer` ni los adaptadores.
No crea `ProductoProveedor`, vincula el canónico con `Product`, persiste asociaciones,
crea identidades desde ofertas ni conecta el matching a las pantallas. Tampoco implementa
RapidFuzz, guardas M5c, conversión de moneda o un adaptador Exel.

## Modelo y carga

Migración: `catalog/0004_producto_canonico`. La carga inicial es manual mediante el
admin de Django o fixtures curados. Los campos MPN y SKU de fabricante se rellenan
solo cuando su significado está confirmado; un código interno no debe cargarse allí.

| Campo | Tipo y regla |
| --- | --- |
| `id` | Identificador automático |
| `marca` | Texto original, obligatorio, máximo 80 |
| `marca_norm` | Derivado con `normalizar`, indexado, no editable |
| `modelo` | Texto original opcional, máximo 120 |
| `modelo_norm` | Derivado con `normalizar_clave`, indexado, no editable |
| `mpn` | MPN original confirmado, opcional, máximo 120 |
| `mpn_norm` | Derivado con `normalizar_clave`, indexado, no editable |
| `sku_fabricante` | SKU original confirmado, opcional, máximo 120 |
| `sku_norm` | Derivado con `normalizar_clave`, indexado, no editable |
| `descripcion` | Texto original obligatorio, sin límite de 200 caracteres |
| `descripcion_norm` | Derivado con `normalizar`, sin expansión persistida de sinónimos |
| `categoria` | FK obligatoria al `Category` existente, con PROTECT |
| `created_at`, `updated_at` | Fechas automáticas |

Los campos opcionales ausentes se guardan como `""`, no `None`. Un identificador
proporcionado debe contener letras/números normalizables. Se conserva la escritura
original; no se eliminan signos ni acentos del campo original.

`save()` valida y recalcula los derivados, también con `update_fields`. El admin
normaliza antes de validar. `update()`, `bulk_create()` y `bulk_update()` omiten este
proceso: evitarlos salvo que el consumidor valide y recalcule todos los derivados.

### Duplicados y colisiones

No hay unicidad global de marca/MPN/modelo/SKU ni de sus formas normalizadas.
Dos registros que colisionan se conservan, no se sobrescriben. Incluso dos identidades
duplicadas exactamente generan revisión manual; el equipo debe depurar el catálogo.
Esta política prioriza no asociar una oferta a un registro elegido arbitrariamente.

## Procedencia explícita de identificadores

El contrato actual `ProviderOffer` tiene `mpn`, `brand` y `model`, pero no garantiza
la procedencia de cada campo ni contiene SKU de fabricante separado. No se modifica.
El consumidor utiliza la declaración `IdentificadoresConfiables` de `catalog.matching`:

```python
IdentificadoresConfiables(
    mpn_confirmado=False,
    modelo_confirmado=False,
    sku_fabricante_confirmado=None,
)
```

- `mpn_confirmado=True`: se verificó que `oferta.mpn` es un número de parte válido.
- `modelo_confirmado=True`: se verificó el significado de `oferta.model`; requiere marca.
- `sku_fabricante_confirmado="..."`: código verificado proporcionado explícitamente.

La declaración no es una verificación automática: debe respaldarla documentación
del adaptador o validación del equipo. Por defecto no se autoriza ningún escalón.
No se infiere confianza del nombre del proveedor, de `external_id` ni del JSON `raw`.
No pasar el SKU de Exel como MPN o SKU confirmado sin validar su significado.

## Operaciones disponibles

- `resolver_match(oferta, candidatos, confianza=...)`: resolución en memoria;
  pasar candidatos ya materializados y con identificador interno. No consulta BD.
- `emparejar(oferta, confianza=...)`: carga mediante una consulta SELECT los
  candidatos de claves indexadas, restringidos por marca cuando se informa.
- `await aemparejar(oferta, confianza=...)`: misma decisión mediante
  `sync_to_async(thread_sensitive=True)`, sin ORM síncrono en el event loop.

El resultado `ResultadoMatch` contiene `canonico`, `estado`, `metodo`, `score`,
`motivo` y `candidatos` (tupla ordenada de IDs). No se guarda ninguna decisión.

## Cascada y revisión

Prioridad: MPN confirmado → SKU de fabricante confirmado → marca/modelo confirmado.
Campos ausentes o sin caracteres normalizables no participan. No se lee `raw`, precio,
moneda, stock o `external_id` para decidir identidad. No se expande por sinónimos.

Los campos normalizados localizan candidatos. Para confirmar automáticamente se
requiere marca presente, candidato único, identidad original compatible y ausencia
de conflictos entre los identificadores confiables disponibles.

**Excepción explícita a “primer acierto corta”:** antes de aceptar el primer escalón
se comprueban los demás identificadores confiables. Un conflicto lo anula. Un campo
ausente en el canónico no se considera contradicción.

| Situación | Estado / motivo |
| --- | --- |
| Sin claves confiables utilizables | SIN_MATCH / SIN_IDENTIFICADORES_CONFIABLES |
| Sin candidato | SIN_MATCH / SIN_COINCIDENCIA_DETERMINISTICA |
| Varios candidatos en el primer escalón con acierto | REVISION_MANUAL / MULTIPLES_CANDIDATOS |
| Candidato sin marca informada en la oferta | REVISION_MANUAL / MARCA_AUSENTE |
| Marca normalizada igual, originales diferentes | REVISION_MANUAL / COLISION_DE_MARCA |
| Identificadores apuntan a productos distintos o contradicen al elegido | REVISION_MANUAL / IDENTIFICADORES_CONTRADICTORIOS |
| Clave normalizada igual, originales diferentes | REVISION_MANUAL / COLISION_DE_NORMALIZACION |
| Candidato único sin los riesgos anteriores | AUTO_MATCH / COINCIDENCIA_UNICA_VERIFICADA |

Solo AUTO_MATCH incluye canónico y score 100. Revisión incluye candidatos pero no
un canónico elegido ni puntuación inventada. SIN_MATCH aquí significa ausencia de
asociación determinística; no prohíbe un futuro fallback M5c.

### Comparación conservadora de originales

Se ignoran únicamente mayúsculas/minúsculas y espacios exteriores. No se declaran
equivalentes otros separadores, acentos ni espacios internos por intuición.

```text
" MPN-1 " / "mpn-1" → asociación posible
"MPN-1" / "MPN 1"   → revisión hasta aprobar una equivalencia
"ABC-P(C)" / "ABC-PC" → revisión; las claves normalizadas colisionan
```

Así se conserva la capacidad de localizar candidatos de M5a sin convertir toda
igualdad normalizada en identidad garantizada. Es una política más conservadora
que el diseño inicial y puede reducir las asociaciones automáticas.

## Ejemplo de consumo (sintético)

```python
from decimal import Decimal
from apps.providers.contracts import ProviderOffer
from apps.catalog.matching import IdentificadoresConfiables, emparejar

oferta = ProviderOffer(
    provider_code="proveedor_sintetico", external_id="123",
    name="Equipo de prueba", price=Decimal("382.533200"), currency="MXN",
    brand="Marca X", mpn="MPN-1",
)
resultado = emparejar(
    oferta, confianza=IdentificadoresConfiables(mpn_confirmado=True),
)
```

Debe existir un canónico curado para obtener coincidencia. Este ejemplo no escribe
en BD, no representa una respuesta real de Exel y no usa sus credenciales.

## Validación

Con PostgreSQL local y `.env` configurado, desde la raíz:

```bash
source venv/bin/activate
python manage.py migrate
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test apps.catalog
python manage.py test
python -m unittest discover -s tools -p 'test_*.py' -v
```

Las pruebas usan fixtures sintéticos y transportes de prueba; no requieren llamadas
reales a SYSCOM o Exel. La creación de candidatos de prueba no carga datos en el
catálogo de desarrollo.

## Trabajo posterior separado

1. Revisar el adaptador SYSCOM: la documentación compartida admite objeto o arreglo
   en búsqueda por modelo, mientras `_producto()` actual solo acepta objeto.
2. Definir persistencia y auditoría de asociaciones (`ProductoProveedor`) y enlace
   con `Product`, sin modificar las cotizaciones existentes accidentalmente.
3. Confirmar identificadores y búsqueda de Exel cuando haya acceso.
4. Conectar el matching con el flujo de ofertas y, en otra tarea, construir M5c.
