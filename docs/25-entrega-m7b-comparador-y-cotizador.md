# Entrega a frontend (M7b): comparador, cotizador y retiro de Clientes

| | |
|---|---|
| **Para** | Quien hace las plantillas y el JavaScript (M7b) |
| **Tareas** | A. Comparador · B. Cotizador · C. Retirar la pestaña Clientes |
| **Backend que las alimenta** | Vista `cotizar_productos` (M7, [24](24-m7-vistas-async-y-flujo.md)); M8 (guardar cotizaciones) aún sin diseñar |
| **Refina** | La §4 de docs/24: el mismo contrato, contado desde el lado del frontend |

El objetivo es dejar las páginas **listas antes de que llegue el backend que les falta** ("prehechas"):
cada pieza sabe qué datos recibirá y en qué forma, así que conectarla será cambiar un interruptor y no
rehacerla.

## 0. Estado del backend por pieza

| Pieza | Backend | Qué hace el frontend ahora |
|---|---|---|
| Comparador: búsqueda y resultados | ✅ Listo: `GET /cotizaciones/buscar/` (HTML) | Pintar el contexto real (parte A) |
| Cotizador: búsqueda | ⏳ Contrato acordado (§B2), endpoint JSON pendiente | Adaptar `api.js` y sus mocks a ese contrato |
| Cotizador: guardar | ⏳ M8, sin diseñar | Formulario con los campos del modelo; guardado con mock |
| Clientes | 🗑️ Se retira | Quitar la pestaña y pedir los datos del cliente en el cotizador (parte C) |
| Inicio, Resumen (PDF y correo) | ✅ Sin cambios | Nada |

---

## Parte A. Comparador (`templates/comparador.html`)

Página **renderizada por Django**: la vista ya calcula todo y la plantilla solo pinta. No usa `api.js`.

### A1. Una plantilla, dos rutas

| Ruta | Quién la sirve | Contexto | Qué se ve |
|---|---|---|---|
| `/comparador/` | `TemplateView` (`settings/urls.py`) | Ninguno | Solo el formulario |
| `/cotizaciones/buscar/?…` | Vista async `cotizar_productos` | Completo (§A3) | Resultados, avisos y errores |

- Mantener el **nombre y la ruta** del archivo y seguir extendiendo `base.html`: las pruebas de M7
  comprueban que se usa `comparador.html`.
- **No hace falta** una plantilla aparte para "sin proveedores": el 503 llega aquí mismo con `error`.
- Plantillas de Django, nunca Jinja2. Sin filtros de apps que no están instaladas (p. ej. `humanize`).

### A2. Formulario de búsqueda

```django
<form method="get" action="{% url 'cotizar_productos' %}">
  <input name="q" value="{{ request.GET.q }}" placeholder="monitor 27 pulgadas">
  <input name="sku" value="{{ request.GET.sku }}" placeholder="Modelo exacto (opcional)">
  <select name="categoria">
    <option value="">Todas</option>
    {% for c in categorias %}
      <option value="{{ c.pk }}" {% if request.GET.categoria == c.pk|stringformat:"d" %}selected{% endif %}>{{ c.name }}</option>
    {% endfor %}
  </select>
  <input name="tipo_cambio" value="{{ request.GET.tipo_cambio }}" inputmode="decimal"
         pattern="[0-9]+(\.[0-9]+)?" placeholder="17.50">
  <button type="submit">Buscar</button>
</form>
```

| Campo | Regla |
|---|---|
| `q` / `sku` | Al menos uno. Si llega `sku`, manda sobre `q` (búsqueda exacta por modelo) |
| `categoria` | Opcional. Es el id **interno** de la categoría (`c.pk`), nunca el nombre ni un id de SYSCOM |
| `tipo_cambio` | Pesos por dólar, con **punto** decimal (`17.50`); `17,50` da error. En la práctica es obligatorio, porque SYSCOM cotiza en dólares |
| Método | GET, sin token CSRF. `request.GET` sirve para volver a rellenar el formulario |

En `/comparador/` no hay contexto, así que el select de categorías sale vacío en la primera carga (§E).

### A3. Variables de contexto

En `/cotizaciones/buscar/` llegan **todas, en todas las respuestas**:

| Variable | Contenido |
|---|---|
| `ofertas` | Lista de ofertas **ya ordenada de más barata a más cara** en pesos. Vacía si hay error |
| `proveedores_caidos` | Códigos de los proveedores que fallaron (`["syscom"]`) |
| `proveedores_caidos_nombres` | Sus nombres legibles, en el mismo orden (`["Syscom"]`): úsalos en el aviso |
| `tipo_cambio` | El usado, o `None`: `tipo_cambio.valor`, `tipo_cambio.origen` (`manual`) y `tipo_cambio.fecha` (UTC) |
| `categorias` | Categorías activas, ordenadas por nombre: `c.pk` y `c.name` |
| `error` | Solo si algo salió mal (400, 429 o 503): texto ya escrito para el usuario |

Cada oferta `o` de `ofertas`:

| En la plantilla | Qué es |
|---|---|
| `o.oferta.name` | Título del producto |
| `o.oferta.brand` / `o.oferta.model` / `o.oferta.mpn` | Marca, modelo y número de parte (pueden venir vacíos) |
| `o.proveedor_nombre` | Nombre legible del proveedor (`Syscom`) |
| `o.oferta.provider_code` | Código (`syscom`): útil para clases CSS o logos (`static/img/syscom.svg`) |
| `o.precio_comparable` | Precio en **pesos (MXN)** sin redondear: `{{ o.precio_comparable\|floatformat:2 }}` |
| `o.oferta.price` y `o.oferta.currency` | Precio **original** del proveedor (p. ej. `10.25 USD`), solo como referencia |
| `o.oferta.stock` | `None` = no informado; `0` = sin existencias; un número = existencias (indicativas) |
| `o.oferta.external_id` | Id del producto en el proveedor |
| `o.es_mejor_precio` | La más barata **de su proveedor**. La más barata de todas es siempre la primera |
| `o.match.estado` | `AUTO_MATCH`, `REVISION_MANUAL` o `SIN_MATCH`; `{{ o.match.estado.label }}` da el texto |
| `o.match.canonico.descripcion` | Producto del catálogo interno, solo con `AUTO_MATCH` (también `.marca` y `.modelo`) |
| `o.match.score` / `o.match.motivo` | Puntuación y motivo de la decisión (para depurar o un tooltip) |

### A4. Situaciones que debe pintar

| Situación | Cómo se reconoce | Qué mostrar |
|---|---|---|
| Página inicial | `ofertas` no existe (ruta `/comparador/`) | Solo el formulario |
| Resultados | `ofertas` tiene elementos | Tarjetas o lista en el orden recibido |
| Modo degradado | Resultados y `proveedores_caidos_nombres` no vacía | Resultados **y** un aviso con los nombres |
| Sin resultados | Hubo búsqueda (`request.GET.q` o `sku`), sin `error` y `ofertas` vacía | "No se encontraron productos" |
| 400 / 429 / 503 | `error` | El mensaje. En el 503, `proveedores_caidos_nombres` puede venir vacía (ningún proveedor activo) |

Esqueleto orientativo (el diseño es libre; se comprobó que renderiza):

```django
{% if error %}
  <div class="alert alert-danger">{{ error }}</div>
{% endif %}
{% if proveedores_caidos_nombres and not error %}
  <div class="alert alert-warning">
    No respondieron: {{ proveedores_caidos_nombres|join:", " }}. Se muestran los demás proveedores.
  </div>
{% endif %}
{% if tipo_cambio %}
  <p>Tipo de cambio: {{ tipo_cambio.valor }} MXN/USD ({{ tipo_cambio.origen }}, {{ tipo_cambio.fecha|date:"d/m/Y H:i" }} UTC)</p>
{% endif %}

{% for o in ofertas %}
  <div class="card {% if forloop.first %}border-success{% endif %}">
    <h5>{{ o.oferta.name }}</h5>
    <p>{{ o.proveedor_nombre }} · {{ o.oferta.brand|default:"" }} {{ o.oferta.model|default:"" }}</p>
    <p class="fs-4">${{ o.precio_comparable|floatformat:2 }} MXN</p>
    <small>Original: {{ o.oferta.price }} {{ o.oferta.currency }}</small>
    {% if o.oferta.stock is None %}<span>Disponibilidad no informada</span>
    {% elif o.oferta.stock == 0 %}<span>Sin existencias</span>
    {% else %}<span>{{ o.oferta.stock }} en existencia</span>{% endif %}
    {% if o.match.estado == "AUTO_MATCH" %}<span>Identificado: {{ o.match.canonico.descripcion }}</span>
    {% elif o.match.estado == "REVISION_MANUAL" %}<span>Posible coincidencia: requiere revisión</span>{% endif %}
    <a href="{% url 'cotizador' %}?sku={{ o.oferta.model|urlencode }}&tipo_cambio={{ request.GET.tipo_cambio|urlencode }}">Agregar a cotización</a>
  </div>
{% empty %}
  {% if request.GET.q or request.GET.sku %}{% if not error %}<p>No se encontraron productos.</p>{% endif %}{% endif %}
{% endfor %}
```

### A5. "Agregar a cotización" desde el comparador

Es un **enlace** al cotizador con el modelo y el tipo de cambio:
`/cotizador/?sku=<modelo>&tipo_cambio=<valor>`. El cotizador, al cargar, lanza esa búsqueda exacta y
muestra el producto con su **precio actual** para añadirlo a la tabla (§B2).

No se pasan precios por la URL ni por `localStorage`: el precio siempre lo da el backend en el momento.
Si la oferta no tiene modelo (`o.oferta.model` vacío), el botón no se muestra.

---

## Parte B. Cotizador (`templates/cotizador.html` + `static/js/cotizador.js`)

Esta página **sí** es JavaScript con `api.js`. Se queda con `USE_MOCKS = true` hasta que exista cada
endpoint, pero sus mocks y su código pasan a usar **exactamente** los contratos de esta parte.

### B1. Cabecera de la cotización

Los datos del cliente se capturan **en el propio cotizador** (la pestaña Clientes se retira, parte C).
Son los campos que ya tiene el modelo `Quote`:

| Campo del formulario | Campo de `Quote` | Regla |
|---|---|---|
| Razón social | `client_name` | Obligatorio, hasta 255 caracteres |
| RFC | `client_rfc` | Opcional. Formato oficial: 12 o 13 caracteres, en mayúsculas y sin guiones (`^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$`) |
| Correo | `client_email` | Opcional, correo válido |
| Vigencia (días) | `valid_days` | Entero; por defecto 15 |
| Notas / condiciones | `notes` | Opcional |
| Tipo de cambio | — (se congelará en M8) | **Uno por cotización**, con punto decimal. Todas las búsquedas lo envían |
| Folio | `folio` | **Lo genera el servidor** (`COT-AAAA-NNNN`): mostrar "Se asigna al guardar", nunca inventarlo |

Si se cambia el tipo de cambio con partidas ya en la tabla, avisar y volver a buscar sus precios: una
cotización no puede mezclar tipos de cambio.

### B2. Búsqueda: `apiClient.products.search()`

**Contrato acordado; el endpoint está pendiente en el backend.** Es la misma búsqueda de M7, en JSON:

`GET /api/v1/products/search/?q=&sku=&categoria=&tipo_cambio=` (mismos parámetros y reglas que §A2).

Respuesta (también en los errores, con el mismo formato):

```json
{
  "success": true,
  "error": null,
  "count": 1,
  "data": [
    {
      "provider": "syscom",
      "provider_label": "Syscom",
      "external_id": "123456",
      "name": "Monitor 27\" Full HD",
      "brand": "Marca",
      "model": "M27-FHD",
      "mpn": "M27-FHD",
      "price_original": "10.25",
      "currency": "USD",
      "precio_mxn": "179.3750",
      "es_mejor_precio": true,
      "stock": null,
      "match": {
        "estado": "AUTO_MATCH",
        "estado_label": "Coincidencia automática",
        "canonico": { "id": 4, "descripcion": "Monitor 27 FHD", "marca": "Marca", "modelo": "M27-FHD" },
        "score": 100,
        "motivo": "COINCIDENCIA_UNICA_VERIFICADA"
      }
    }
  ],
  "proveedores_caidos": ["ct"],
  "proveedores_caidos_nombres": ["CT Online"],
  "tipo_cambio": { "valor": "17.50", "origen": "manual", "fecha": "2026-10-12T18:00:00Z" }
}
```

| Regla | Detalle |
|---|---|
| Dinero como **texto** | `price_original`, `precio_mxn` y `tipo_cambio.valor` llegan como cadenas para no perder precisión (el backend nunca usa `float`). Convertir solo para mostrar o calcular en pantalla |
| Orden | `data` ya viene de más barata a más cara: no reordenar |
| Errores | 400, 429 y 503 traen `"success": false`, el mensaje en `error` y `data: []`. **Cambiar `api.js`** para que en esos códigos lea el JSON en lugar de lanzar `Error HTTP` |
| `match.canonico` | `null` salvo con `AUTO_MATCH` |
| `estado_label` | `Coincidencia automática`, `Revisión manual` o `Sin coincidencia` |
| Filtro por proveedor | El select `providerFilter` filtra **en el navegador** sobre `provider`; el backend no tiene ese parámetro |

Qué cambia respecto a los mocks actuales de `api.js`:

- **Desaparecen** `price_wholesale_mxn`, `suggested_margin`, `warranty_months`, `specs`, `category`,
  `clave`, `description` y el objeto `comparison`: ningún proveedor los da. La comparación **es** la
  lista ordenada.
- **Desaparece** `products.getComparison()` (`/products/{sku}/comparison/`): la búsqueda por `sku` ya
  devuelve todos los proveedores de ese modelo.
- **Reescribir `MOCK_PRODUCTS`** con la forma de arriba (incluido un caso con `proveedores_caidos`), para
  que el día que se ponga `USE_MOCKS = false` no cambie nada más.

### B3. Partidas y totales

Cada partida guarda: `provider`, `external_id`, `name` (descripción), `model`, `precio_mxn` (costo),
`margen` (%) y `cantidad`.

| Concepto | Cálculo (en pantalla) |
|---|---|
| Precio unitario de venta | `precio_mxn × (1 + margen / 100)` |
| Importe de la partida | `precio unitario × cantidad` |
| Subtotal | Suma de importes |
| IVA | `subtotal × 0.16`, **sumado aparte** |
| Total | `subtotal + IVA` |

- **Corregir el cálculo actual del IVA:** hoy `cotizador.js` hace `importe / 1.16`, como si los precios ya
  incluyeran IVA. El backend calcula `tax = subtotal × tax_rate` sobre precios **sin** IVA (SYSCOM se
  consulta con `iva=0`).
- Redondear a 2 decimales solo al mostrar. Al guardar (M8), los importes los recalcula el servidor y
  mandan los suyos.
- El margen por partida **todavía no existe en el modelo** `QuoteItem`: se decide en M8.

### B4. Guardar: `apiClient.quotes.save()`

**Pendiente de M8: sigue con mock.** Para no rehacerlo después, el payload ya debe tener esta forma:

```json
{
  "client_name": "Constructora del Norte S.A. de C.V.",
  "client_rfc": "CIN980415AA2",
  "client_email": "compras@cinorte.mx",
  "valid_days": 15,
  "notes": "",
  "tipo_cambio": "17.50",
  "items": [
    { "provider": "syscom", "external_id": "123456", "description": "Monitor 27\" Full HD",
      "model": "M27-FHD", "quantity": "2", "costo_mxn": "179.3750", "margen": "20" }
  ]
}
```

- **Sin** `folio`, `client_id`, totales ni IVA: el servidor genera el folio y calcula los importes.
- Números como texto, igual que en la búsqueda.
- Es un **borrador**: M8 puede ajustarlo (sobre todo el margen y cómo se congela el tipo de cambio). Lo
  que no cambiará son los campos del cliente, que ya existen en `Quote`.

### B5. Estados de la búsqueda en el cotizador

Cargando (skeleton actual) · resultados · modo degradado (aviso con `proveedores_caidos_nombres`) · sin
resultados · error (`error` del JSON: falta el tipo de cambio, demasiadas búsquedas o ningún proveedor
disponible).

Al cargar con `?sku=…&tipo_cambio=…` (viene del comparador, §A5): rellenar ambos campos y lanzar la
búsqueda automáticamente.

---

## Parte C. Retirar la pestaña Clientes

No hay modelo de clientes: `Quote` guarda los datos del cliente directamente (§B1). Pasos, **en este
orden** (si se quita la ruta antes que los enlaces, todas las páginas fallan con `NoReverseMatch`):

1. `templates/base.html`: quitar el enlace del menú (`{% url 'clientes' %}`, "CLIENTES") y el del pie
   ("Gestión de Clientes").
2. `templates/cotizador.html`: sustituir el select `clientSelect` y el modal `newClientModal` por los
   campos del cliente de §B1.
3. `static/js/cotizador.js`: quitar la carga y el alta de clientes (`apiClient.clients`, `clientSelect`,
   el parámetro `?cliente=`) y tomar los datos del cliente de los campos nuevos.
4. `static/js/api.js`: quitar el servicio `clients`, `INITIAL_MOCK_CLIENTS` y `STORAGE_KEYS.CLIENTS`.
5. Borrar `templates/clientes.html` y `static/js/clientes.js`.
6. `settings/urls.py`: quitar la línea `path('clientes/', …, name='clientes')`. Es la **única** línea de
   backend que se toca; avisar a backend en el PR.
7. Comprobar que no queda ninguna referencia: buscar `clientes`, `clients` y `clientSelect` en
   `templates/` y `static/`.

---

## Parte D. Reglas comunes de presentación

Vienen de M5, M6 (docs/22 y docs/23) y del acuerdo de uso de SYSCOM (docs/02 §8.2):

- **Precio:** el principal es el precio en pesos (`precio_comparable` / `precio_mxn`). El original
  (`price` + `currency`) solo como referencia.
- **Existencias:** `None`/`null` y `0` son cosas distintas. El mejor precio **no** promete
  disponibilidad.
- **Emparejamiento:**
  - `AUTO_MATCH` → "Producto identificado".
  - `REVISION_MANUAL` → "Posible coincidencia, requiere revisión". **Nunca** presentarla como confirmada.
  - `SIN_MATCH` → no mostrar nada.
- **Proveedores caídos:** nombrarlos en el aviso, sin detalles técnicos (no llegan a propósito).
- **Precios de distribuidor:** solo uso interno de CCONOR. No publicarlos ni guardarlos en el navegador.
- **Tipo de cambio:** mostrar cuál se usó (valor, origen y fecha) junto a los precios.
- **Dinero:** nunca calcular el ranking ni el orden en JavaScript; solo totales de pantalla en el
  cotizador (§B3).

## Parte E. Cómo probar y pendientes

**Probar:**

1. Una sola vez: `venv\Scripts\python.exe manage.py migrate`. Después: `manage.py runserver`.
2. Comparador: `/comparador/` → formulario; buscar con tipo de cambio (`monitor`, `17.50`) → resultados
   reales. ⚠️ **Cada búsqueda válida hace 1 petición real a SYSCOM** (credenciales de producción):
   busca lo justo.
3. Sin tipo de cambio → 400; búsqueda vacía → 400; once búsquedas en un minuto → 429 (no consultan a
   SYSCOM). Para ver el 503 sin gastar peticiones: desactivar `syscom` en el admin y reactivarlo al acabar.
4. Cotizador: con `USE_MOCKS = true`, los mocks nuevos deben recorrer todos los estados de §B5.
5. Clientes: `/clientes/` debe dar 404 y ninguna página debe fallar.
6. `venv\Scripts\python.exe manage.py test apps.quotes` debe seguir en verde.

**Pendientes:**

| Pendiente | Responsable |
|---|---|
| Endpoint JSON `GET /api/v1/products/search/` (§B2) | Backend (M7) |
| Guardar cotizaciones: endpoint, margen por partida y congelación del tipo de cambio (§B4) | Backend (M8) |
| `/comparador/` sin contexto: el select de categorías sale vacío en la primera carga | Backend y equipo |
| Sesión: SYSCOM recomienda mostrar sus precios solo a usuarios con sesión | Equipo (docs/24 §9) |
| Tipo de cambio automático en lugar de la captura manual | Backend (docs/24 §9) |
