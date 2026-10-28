# 34 · Comparador con carga diferida: SYSCOM primero, Exel después

**Fecha:** 2026-10-28 · **Rama:** `fix/mejora-de-web-scraping`
**Refina:** docs/26 (vista del comparador) y docs/14 (`search_all` gana el parámetro `codes`). No cambia el cotizador ni la API `/api/v1/products/search/`.

## 1. Para qué sirve

El comparador se generaba en el servidor y esperaba a **todos** los proveedores (`search_all` → `asyncio.gather`), así que lo marcaba el más lento. SYSCOM responde en ~1 s, pero Exel (web scraping) tarda ~4 s en una búsqueda nueva, aun con la caché y los lotes de docs/32 §11. El asesor veía la página en blanco ~4 s.

Ahora la página llega con los proveedores rápidos y los lentos se piden después desde el navegador. Sus tarjetas se intercalan en el mismo orden que usa la vista.

## 2. Qué proveedores van «después»

```python
# settings/settings.py
CCONOR_PROVEEDORES_DIFERIDOS = ('exel',)
```

- Un `code` de la lista se carga después **solo en el comparador**. Si el proveedor no está activo, se ignora.
- Los demás proveedores activos son los «inmediatos» y salen con la página.
- Si no hay ningún diferido activo, el comparador funciona exactamente como antes: una sola consulta a todos.

## 3. Cómo funciona una búsqueda

```
GET /comparador/?q=camara ip                       (cuenta en el límite de búsquedas, como siempre)
  └─ cotizar_productos
       ├─ _reparto_de_proveedores() → inmediatos ["syscom"], diferidos [("exel", "Exel del Norte")]
       ├─ buscar(request, codes=inmediatos, parcial=True)   → search_all(query, codes=["syscom"])
       ├─ _registrar_pendiente() → sesión["comparador_pendientes"][id] = {params, tipo_cambio, codes}
       └─ comparador.html: tarjetas de SYSCOM + «Buscando en Exel del Norte… puede tardar unos segundos.»

static/js/comparador.js  (si existe #comparadorPendiente)
  └─ GET /comparador/diferidos/<id>/       (de un solo uso; no cuenta en el límite)
       └─ comparador_diferidos
            ├─ saca el pendiente de la sesión (si no está → 404 «Vuelve a buscar»)
            ├─ buscar(request, tipo de cambio guardado, codes=["exel"], contar=False, parcial=True)
            ├─ _recordar_ofertas_vistas()  → «Agregar» a la bandeja acepta las ofertas de Exel
            └─ JSON {html: tarjetas, ofertas: n, caidos: [nombres], error: texto | null}
  └─ intercala cada tarjeta, recoloca «Otros resultados relacionados», quita «Buscando…»,
     añade los caídos al aviso y, si no queda ninguna oferta, muestra «No se encontraron productos»
     (o «Ningún proveedor pudo responder…» si cayeron todos)
```

### 3.1 Piezas

| Pieza | Dónde | Qué hace |
|---|---|---|
| `search_all(query, *, transport=None, codes=None)` | `apps/providers/service.py` | Con `codes`, solo consulta esos proveedores activos; con `[]`, ninguno. Con `None`, igual que antes |
| `buscar(request, tipo_cambio_fijo=None, *, codes=None, contar=True, parcial=False)` | `apps/quotes/views.py` | `codes` va a `search_all`. `contar=False` no suma en el límite. `parcial=True`: sin proveedores o con todos caídos no da 503, sino 200 con el aviso de caídos |
| `_reparto_de_proveedores`, `_registrar_pendiente` | `apps/quotes/views.py` | Separan inmediatos de diferidos y guardan el pendiente (`CLAVE_PENDIENTES`, como mucho `MAX_PENDIENTES` = 5, por si hay varias pestañas) |
| `comparador_diferidos` | `apps/quotes/views.py`, ruta `comparador_diferidos` | El endpoint del paso diferido |
| `_comparador_oferta.html` | `templates/` | La tarjeta de una oferta, la misma en la página y en el JSON. Lleva el orden en `data-nivel`, `data-precio` (MXN), `data-proveedor`, `data-id` y `data-sin-coincidencia` |
| `comparador.html` | `templates/` | Usa el partial, contiene `#comparadorOfertas` (con `data-proveedores`), `#comparadorPendiente`, el aviso de caídos (`#comparadorAvisoCaidos`, oculto si está vacío), `#comparadorSinResultados` (oculto mientras haya pendiente) y la plantilla del separador |
| `comparador.js` | `static/js/` | Hace el `fetch`, intercala las tarjetas y lanza `comparador:ofertas` |
| `main.js` | `static/js/` | Al recibir `comparador:ofertas`, repinta la bandeja para que las tarjetas nuevas marquen «En la bandeja · n» |

### 3.2 Orden

Las tarjetas se ordenan por (nivel de coincidencia, precio en MXN, `provider_code`, `external_id`): la misma clave que `buscar` usa al ordenar `OfertaPresentada`. Las que llegan después se insertan en ese orden. El separador «Otros resultados relacionados» se coloca antes de la primera tarjeta sin coincidencia. «Mejor precio» sigue siendo por proveedor (M6), así que no hay que recalcularlo al juntar.

## 4. Seguridad y límites

- **Un solo uso y por sesión.** El id es aleatorio (`uuid4`), vive en la sesión del asesor y se borra al usarse. Otro usuario no puede usarlo, y repetirlo da 404.
- **Datos del servidor, no del navegador.** El paso diferido usa los `params` (`q`, `sku`, `categoria`) y el tipo de cambio que guardó la página; la URL solo lleva el id. Así los precios en USD de un proveedor diferido se convierten con el mismo tipo de cambio que vio el asesor.
- **Límite de búsquedas.** La página cuenta una vez, como antes; el paso diferido no cuenta. No se puede abusar de él, porque sin un pendiente creado por la página no hace nada.
- **Sesión caducada.** `login_required` redirige al acceso; el JS no recibe JSON y muestra «No se pudo consultar …».
- **Sin JavaScript** solo se ven los inmediatos y «Buscando…» se queda fijo. Es una herramienta interna con JS obligatorio (la bandeja ya lo exige).

## 5. Errores

| Caso | Página | Paso diferido |
|---|---|---|
| SYSCOM cae y Exel pendiente | 200, aviso «No respondieron: SYSCOM», «Buscando…» | — |
| Solo hay proveedores diferidos activos | 200 sin ofertas, «Buscando…» | — |
| Exel cae (timeout, sesión, etc.) | — | 200, `caidos: ["Exel del Norte"]`; el JS lo añade al aviso |
| Cayeron todos y no hay ofertas | — | El JS muestra «Ningún proveedor pudo responder…» |
| Proveedor diferido en USD sin tipo de cambio, o fallo del proceso | — | 200 con `error`; el JS lo muestra en lugar de «Buscando…» |
| Id usado, desconocido o desplazado por más de 5 búsquedas | — | 404 «Esta búsqueda ya no está disponible. Vuelve a buscar.» |
| Entrada inválida (400) o límite (429) | Como antes; no se crea pendiente | — |

## 6. Cómo se conecta

- **M2 (`apps/providers`):** solo el parámetro `codes` de `search_all`. Timeout, reintento ante 401 y errores como datos siguen igual.
- **M6 (`quotes.comparison`) y M5 (`catalog.matching`):** se aplican en cada paso a sus propias ofertas, igual que antes. La comparación ya era por proveedor.
- **Bandeja (19A):** solo acepta ofertas vistas. El paso diferido registra las suyas en `comparador_ofertas_vistas`.
- **Última búsqueda (22A):** se guarda en la página, como antes. Al volver al comparador, se repite con su propio paso diferido.
- **Caché de Exel (docs/32 §11):** si la búsqueda se repite antes de 5 min, el paso diferido responde casi al instante.
- **No cambia:** el cotizador (`cotizador.js`) ni la API `/api/v1/products/search/`, que siguen consultando a todos a la vez.

## 7. Cómo añadir un proveedor a la carga diferida

1. **¿Conviene?** Sí, si su búsqueda tarda claramente más que la de los demás (en la práctica, más de ~2 s) y su espera retrasa la página. Mídelo como en docs/32 §11 (`elapsed_ms` de `search_all`) dentro de un presupuesto de peticiones aprobado. Un proveedor rápido no gana nada y obliga a una petición más.
2. **Añádelo** a `CCONOR_PROVEEDORES_DIFERIDOS` en `settings/settings.py`, con un comentario que diga por qué (cuánto tarda). Por ejemplo: `('exel', 'nuevo')`.
3. **Nada más:** no hace falta tocar vistas, plantillas ni JS. El texto «Buscando en …» usa `Provider.name`.
4. **Pruebas:** las de `CargaDiferidaTests` (`apps/quotes/tests/test_vistas_cotizacion.py`) cubren el mecanismo con proveedores ficticios. Si una prueba del comparador usa ese `code` y espera sus ofertas en la página, ponle `@override_settings(CCONOR_PROVEEDORES_DIFERIDOS=())`, como `test_v10c`.
5. **Documenta** la decisión en el SDD del proveedor y en `MEMORY.md`.

Para quitar uno de la carga diferida, basta con sacarlo de la lista.

## 8. Pruebas

- `apps/providers/tests/test_service.py`: `test_codes_consulta_solo_esos_activos_y_vacio_a_ninguno`.
- `apps/quotes/tests/test_vistas_cotizacion.py`, `CargaDiferidaTests` D1–D11. Cubren:
  - solo los inmediatos en la página;
  - el pendiente en la sesión;
  - las tarjetas del endpoint con sus `data-*`;
  - el un solo uso;
  - la bandeja;
  - el límite;
  - el tipo de cambio guardado;
  - los caídos;
  - nada de 503 mientras hay pendientes;
  - no hay pendiente con 400 o 429 ni en un formulario en blanco;
  - el setting vacío;
  - 404 y el acceso sin sesión;
  - el máximo de 5 pendientes.
- 16/16 mutaciones detectadas: filtro `codes`, `contar`, `parcial`, el reparto, el un solo uso, las ofertas vistas, el tipo de cambio guardado, el máximo de pendientes, los caídos y el «sin resultados» oculto.
- El JS no tiene pruebas automáticas (el proyecto no tiene runner de JS ni paso de build): sintaxis comprobada con `node --check` y prueba manual en el navegador (§9).

## 9. Prueba manual

Con `runserver`, buscar «camara ip» en el comparador:
1. SYSCOM sale en ~1 s, con «Buscando en Exel del Norte…» debajo.
2. Las tarjetas de Exel aparecen después, intercaladas por precio y antes de «Otros resultados relacionados» si coinciden con la búsqueda.
3. «Agregar» funciona con una oferta de Exel, y las que ya estaban en la bandeja muestran «En la bandeja · n».
4. Al repetir la búsqueda, Exel llega casi al instante (caché de 5 min).
