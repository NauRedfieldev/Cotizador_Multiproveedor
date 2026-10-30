# 35 · M12: calibración del matching (en espera)

**Fecha:** 2026-10-30 · **Estado:** en espera, sin cambios de código
**Relacionado:** [21 · Catálogo canónico y M5b](21-catalogo-canonico-y-m5b.md) y [22 · M5c: sugerencias y guardas](22-m5c-fallback-y-guardas.md).

## 1. Qué es M12

M12 consiste en medir con datos reales si los umbrales del matching por texto (M5c) son correctos y ajustarlos:

```python
# apps/catalog/constants.py (provisionales)
UMBRAL_AUTO_MATCH = 90       # hoy solo es una señal: no habilita auto-match por texto
UMBRAL_REVISION_MANUAL = 70  # desde aquí se sugiere el candidato para revisión manual
```

El plan original pedía un script de calibración y unos 50–100 pares reales etiquetados por personas. Antes de hacerlo se midió si, con SYSCOM y Exel, calibrar cambiaría algo.

## 2. Decisión

**M12 queda en espera.** Con los proveedores actuales:

- los umbrales no influyen en ningún resultado (§4.2);
- el matching nunca dio un falso positivo;
- comparar solo por texto no distingue productos con ningún umbral (§4.3).

Se mantienen 70/90 y la regla de docs/22: **nunca hay auto-match por texto**.

Retomar M12 cuando ocurra alguna de estas condiciones:

1. Se integra un proveedor nuevo que vende las mismas marcas que otro (por ejemplo Ubiquiti o Hikvision junto a SYSCOM).
2. Hay unos meses de cotizaciones reales de CCONOR para sacar la muestra.
3. Un proveedor manda ofertas **sin modelo ni MPN**, que es el único caso en que el texto decide.

## 3. Método

Todo fue de solo consulta: no se escribió en la BD ni se cambió código.

1. **Marcas en común.** Se buscaron 48 marcas en ambos proveedores con `search_all(ProductQuery(term=…), codes=["syscom", "exel"])` y se contó la marca real de cada oferta, no solo el número de resultados.
2. **Barrido profundo.** Se hicieron 44 búsquedas de las marcas comunes (switches, Omada, Tapo, VIGI, Archer, no-breaks, PDU, discos Purple/Red…), con 3 s de pausa entre búsquedas.
3. **Simulación.** Los productos de SYSCOM de esas marcas se tomaron como catálogo canónico: 434 `ProductoCanonico` en memoria, con `clean_fields` para los campos derivados. Cada oferta de Exel (542) pasó por `resolver_con_fallback`, sin sinónimos y con la categoría asignada por palabras clave del nombre.
4. **Respuesta correcta.** Se usó el mismo código de modelo/MPN normalizado o, solo en TP-Link, el mismo código sin el prefijo `TL-`. Es una aproximación: no hubo etiquetado manual.

Se corrieron dos escenarios:

- **A**, igual que en producción: Exel con MPN y modelo confiables, como en `CONFIANZA_POR_PROVEEDOR`.
- **B**, solo texto: sin identificadores, para medir el fuzzy puro.

## 4. Resultados

### 4.1 Marcas en común

| Marca | SYSCOM | Exel | Mismo código |
| --- | ---: | ---: | ---: |
| TP-Link | 264 | 400 | 176 |
| CyberPower | 114 | 54 | 40 |
| Western Digital | 56 | 88 | 16 |
| Huawei | 122 | 68 | 0 |

- Exel no vende Ubiquiti, Hikvision, Ruijie, Cisco, MikroTik ni Grandstream: al buscar solo la marca devuelve 0. Si se escribe «switch ubiquiti», ignora la marca y devuelve sus switches genéricos.
- SYSCOM no vende Dahua, APC, Tripp Lite, Dell, HP, Epson ni Kingston: al buscarlas devuelve productos de otras marcas que mencionan la palabra.
- Huawei: cada proveedor maneja líneas distintas, sin productos comunes.

### 4.2 Escenario A (producción)

| Ofertas de Exel | Resultado | Cantidad |
| --- | --- | ---: |
| Con gemelo de mismo código | AUTO_MATCH correcto | 130 |
| Con gemelo de mismo código | Revisión manual con el correcto (`COLISION_DE_NORMALIZACION`) | 86 |
| Con gemelo de mismo código | Sin match (marca escrita distinto) | 16 |
| Gemelo que solo difiere por `TL-` | Sin match | 33 |
| Sin gemelo | Sin match | 277 |
| Cualquiera | AUTO_MATCH incorrecto o sugerencia falsa | **0** |

Ninguna oferta llegó a la puntuación por texto. `_resolver_fallback` descarta los candidatos cuyo modelo o MPN contradice el de la oferta, y SYSCOM y Exel siempre mandan ambos campos. Por eso los umbrales no se usan.

### 4.3 Escenario B (solo texto)

Puntuación `token_set_ratio` del producto correcto: mediana 55, mínima 25. Las descripciones difieren mucho: SYSCOM escribe en español con fichas largas y Exel muchas veces en inglés.

| Umbral | Correctos que lo pasan | Impostores que lo pasan (pasan guardas) |
| ---: | ---: | ---: |
| 70 | 23 / 177 | 247 / 17 830 |
| 80 | 8 / 177 | 46 / 17 830 |
| 90 | 2 / 177 | 3 / 17 830 |
| 95 | 0 / 177 | 0 / 17 830 |

Ningún umbral separa los productos correctos de los impostores. Ejemplos de impostores con puntuación alta: `TL-SG2218` (16 puertos) contra `SG3210` (8 puertos) con 92, y `WD81PURZ` contra `WD84PURZ` (dos modelos distintos de 8 TB) con 82.

## 5. Huecos encontrados en M5b (fuera de M12)

No son umbrales: son reglas del matching por código o datos del catálogo. Corregirlos requiere cambiar código y una decisión del equipo. Hoy no afectan nada visible porque el catálogo canónico está vacío.

| Caso | Pares | Comportamiento actual | Por qué |
| --- | ---: | --- | --- |
| Modelo con distinta escritura: `ARCHER-T2U-NANO` / `Archer T2U Nano`, `TAPO-C500` / `Tapo C500` | 86 | Revisión manual, con el correcto sugerido | `_firma_original` compara el texto original y lo trata como colisión (diseño de docs/21) |
| TP-Link Omada: `TL-SG3210` / `SG3210` | 33 | Nunca se emparejan | `normalizar_clave` no conoce el prefijo, y el fuzzy descarta códigos distintos |
| Western Digital: `Western Digital (WD)` / `WESTERN DIGITAL (OEM)` | 16 | Nunca se emparejan | El bloqueo exige `marca_norm` idéntica; no hay alias de marca |

## 6. Límites

- Cada búsqueda trae como máximo 60 resultados: no se recorrieron los catálogos completos.
- La respuesta correcta se tomó por código, no por etiquetado manual.
- Las categorías se asignaron por palabras clave y no se usaron sinónimos (`SinonimoRed` está vacío).
- Los catálogos de los proveedores cambian; repetir el barrido antes de retomar M12.
