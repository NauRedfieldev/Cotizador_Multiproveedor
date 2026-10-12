# 18 · Pruebas entre componentes: SYSCOM (M2 + M3) → catalog → quotes (M1b)

| | |
|---|---|
| **Fuente de verdad** | [`apps/quotes/tests/test_integracion_proveedores.py`](../apps/quotes/tests/test_integracion_proveedores.py). Si este documento y el código discrepan, manda el código |
| **Estado** | Implementado y verificado el 2026-10-05: 15 pruebas (I1–I15), 274 pruebas OK en total, 6 de 6 mutaciones detectadas y verificación real con 0 tokens y 2 GET |
| **Se apoya en** | M2 ([14](14-contrato-y-orquestacion.md)), M3 ([17](17-adaptador-syscom.md)) y el DER de catalog y quotes ([DER](DER.md)) |
| **Propuesta derivada** | Conversión USD→MXN en `quotes`: [19](19-propuesta-conversion-usd-mxn.md) |

## 1. Para qué sirven

Hasta ahora cada módulo tenía sus propias pruebas. Estas recorren la cadena completa:

1. una búsqueda en SYSCOM;
2. el distribuidor (`Supplier`) de cada oferta;
3. el producto del catálogo;
4. la partida de la cotización;
5. los totales con IVA.

Así se comprueba que los contratos de cada módulo encajan entre sí. También dejan como evidencia ejecutable las brechas que todavía hay en la cadena (§5).

## 2. Cómo se conectan los módulos

```
ProductQuery(term | sku)
   │
   ▼
M2  service.search_all ──► Provider(active=True) ──► registry ──► M3 SyscomAdapter
   │                                                   │  token: ProviderToken (tokens.py)
   │                                                   │  GET /productos ?busqueda | ?modelo  moneda=usd iva=0
   ▼                                                   ▼
list[ProviderResult] ── ProviderOffer(provider_code, external_id, name, price: Decimal, currency="USD",
   │                                  brand, model, mpn (=modelo), stock, raw)
   │
   ▼  (único enlace en BD entre módulos)
M1b catalog  Supplier.provider (OneToOne, opcional, PROTECT) ──► providers.Provider
   │         Supplier.objects.aget(provider__code=oferta.provider_code)
   │         Product(sku, name varchar(200), category, supplier, unit_price numeric(12,2))  ← sin moneda
   │         normalizer.normalizar_clave(mpn/sku): clave exacta de emparejamiento
   ▼
M1b quotes   QuoteItem.product → Product (SET_NULL); al guardar copia description y unit_price (snapshot)
             Quote.subtotal = Σ cantidad × precio (SQL) · tax = subtotal × tax_rate (0.16) · total
```

- **Dirección de las dependencias:** `catalog` depende de `providers` (por `Supplier.provider`), y `quotes` depende de `catalog` (por `QuoteItem.product`). `providers` no conoce a ninguno de los dos. Por eso las pruebas viven en `quotes`, que está al final de la cadena.
- **Lo que M2 y M3 entregan:** ofertas con precio `Decimal` en USD sin IVA, y `brand`, `model` y `mpn` para emparejar. Los errores llegan dentro de cada `ProviderResult`.
- **Lo que catalog añade:** el distribuidor de cada oferta (`Supplier`) y la clave exacta (`normalizar_clave`).
- **Lo que quotes añade:** el snapshot de precio y descripción, y los totales con IVA.

### 2.1 El flujo de referencia

**Todavía no existe el código que une la cadena.** Faltan el motor de comparación, el emparejamiento, el paso de una oferta a `Product` o `QuoteItem`, la conversión de moneda y las vistas. Por eso las pruebas recorren un flujo de referencia escrito en el propio módulo de pruebas, sin añadir código de producción:

| Paso | Función de prueba | Qué hace |
|---|---|---|
| 1 | `search_all(...)` | Pide las ofertas a todos los proveedores activos |
| 2 | `clave(oferta)` y `emparejar(oferta)` | Clave = `normalizar_clave(mpn or model)`. Busca un único `Product` cuya clave coincida **exactamente** (sin RapidFuzz) |
| 3 | `mas_barata(resultados, clave)` | La oferta de menor `price` con esa clave y la misma moneda, entre los proveedores que respondieron |
| 4–5 | `producto_desde_oferta(oferta, categoria)` | Resuelve el `Supplier` con `aget(provider__code=…)` y crea o actualiza el `Product` con `unit_price = oferta.price` |
| 6 | `QuoteItem.objects.acreate(...)` sin precio | El modelo copia la descripción y el precio del producto (snapshot) |
| 7 | `totales(quote)` | Lee `subtotal`, `tax` y `total` con `sync_to_async` (§5, I12) |

Este flujo es lo que tendrá que implementar el motor de comparación. Las pruebas lo dejan especificado.

## 3. Las pruebas

Todas llevan la etiqueta `django_db`. La base `BaseIntegracion` prepara en `setUp`:
- `exigir_bd_de_pruebas()`, `sin_red()` y `credenciales_de_prueba("syscom", …)`;
- `ApiFalsa`;
- el `Provider` "syscom", un `ProviderToken` guardado (para que no se pida ninguno), un `Supplier` "SYSCOM" enlazado, una `Category` y un asesor.

Las muestras tienen la estructura real de la API y precios ficticios: 900001 `CAM-IP-2MP-A1` a 85.50, 900002 `DOMO-IP-4MP-B2` a 150.25 y 900003 `FUENTE-12V-C3` a 8.75.

| # | Prueba | Resultado esperado | Obtenido |
|---|---|---|---|
| **A** | **De M2/M3 a catalog** | | |
| I1 | Cada oferta resuelve su Supplier | `ok`, 3 ofertas, todas con el Supplier "SYSCOM". Solo `GET /productos`, con 0 peticiones de token | ✅ |
| I2 | Sin Supplier enlazado (estado actual, D10) | Las ofertas llegan, pero `aget` lanza `Supplier.DoesNotExist` | ✅ |
| I3 | Proveedor inactivo con Supplier | `[]`, 0 peticiones y el Supplier sigue enlazado | ✅ |
| I4 | 503 y 429 de SYSCOM | Llegan como `ProviderResponseError` y `ProviderRateLimitError`, sin ofertas y con 0 `Product`/`Quote`/`QuoteItem` | ✅ |
| **B** | **Emparejamiento por clave exacta** | | |
| I5 | `Product` con sku `"CAM IP 2MP A1"` | Solo empareja la 900001; las otras dos dan `None` | ✅ |
| I6 | `?modelo=` y modelo no disponible | 1 oferta que empareja. El 404 `product_not_available` da `ok` con 0 ofertas y no crea nada | ✅ |
| **C** | **De catalog a quotes** | | |
| I7 | 3 × CAM y 2 × DOMO, partidas sin precio | Snapshot de 85.50 y 150.25 con el título. Subtotal 557.00, IVA 89.12, total 646.12. Todo `Decimal` | ✅ |
| I8 | Precio nuevo de SYSCOM (99.99) | El `Product` pasa a 99.99; la partida sigue en 85.50 y los totales no cambian | ✅ |
| I9 | SYSCOM a 85.50 frente a "prueba" a 80.00, mismo modelo | Gana "prueba" a 80.00, con el Supplier "Prueba" | ✅ |
| I10 | "prueba" responde 503 | Su resultado trae el error y la partida se cotiza con SYSCOM a 85.50 | ✅ |
| **D** | **Caracterización de las brechas (§5)** | | |
| I11 | `…el_precio_en_usd_entra_sin_convertir` | 1 × 85.50 USD da subtotal 85.50, IVA 13.68 y total 99.18, como si fueran pesos. Ningún modelo tiene `currency` ni `exchange_rate` | ✅ |
| I12 | `…los_totales_no_se_leen_desde_async` | `quote.subtotal` en `async def` lanza `SynchronousOnlyOperation`; con `sync_to_async` funciona | ✅ |
| I13 | `…un_titulo_de_mas_de_200_caracteres_no_cabe_en_product` | M3 acepta un título de 250 caracteres, pero crear el `Product` lanza `DataError` | ✅ |
| I14 | `…un_precio_con_mas_de_dos_decimales_se_redondea_al_guardar` | 85.505 se mantiene en memoria (Product y snapshot) y queda en 85.51 en la BD. `amount` (`ROUND_HALF_UP`) también da 85.51 | ✅ |
| **E** | **Secretos** | | |
| I15 | Flujo completo con `CENTINELA_TOKEN` | Se usa el token guardado (`Bearer`). El centinela no aparece en `Product`, `QuoteItem` ni en los logs (todos los loggers, DEBUG) | ✅ |

## 4. Cómo se comprobó que se cumplen

| Comprobación | Resultado |
|---|---|
| `manage.py test apps.quotes` | 15 OK |
| `manage.py test` (regresión) | **274 OK** (259 anteriores + 15), sin `expectedFailure` nuevos |
| Red de seguridad | `sin_red()` en todas las clases. Las rutas pedidas se comprueban con `ApiFalsa.rutas_pedidas()` |
| Mutaciones (temporales, restauradas desde memoria) | **6 de 6 detectadas** (tabla de abajo) |
| Verificación real | §4.1 |

| Mutación | La detecta |
|---|---|
| M1 Quitar el snapshot de `unit_price` en `QuoteItem.save` | I7, I8, I9, I10, I11, I12, I14 |
| M2 `tax` = 0 | I7, I11, I12 |
| M3 `CAMPO_PRECIO = "precio_1"` | I7, I8, I10, I11, I12, I14, I15 |
| M4 `search_all` sin `filter(active=True)` | I3 |
| M5 `normalizar_clave` sin quitar separadores | I5 |
| M6 `_money` con `ROUND_HALF_EVEN` | I14. Al principio sobrevivía: ningún importe caía en medio centavo, así que I14 se amplió para cotizar el precio 85.505 |

### 4.1 Verificación contra la API real (2026-10-05)

El presupuesto lo aprobó el usuario: **0 tokens nuevos y 2 GET**. Se usó un script fuera del repositorio, ejecutado con `manage.py shell` contra la BD de desarrollo:
- un transporte que solo anota el método y la ruta;
- las escrituras de catalog y quotes, dentro de `transaction.atomic()` con `set_rollback(True)`.

Solo se publican recuentos, tipos y tiempos.

| # | Petición | Resultado |
|---|---|---|
| 1 | `search_all(ProductQuery(term="camara ip", limit=10))` | ok: 10 ofertas y 0 descartes en 1233 ms. Precios `Decimal` y USD. Como mucho 2 decimales; título más largo de **173** caracteres; modelo más largo de 19 |
| 2 | `search_all(ProductQuery(sku=<mpn de la más barata>))` | ok: 1 oferta en 249 ms, con el mismo `external_id`, la misma clave y el mismo precio |

En la cadena (que luego se revirtió):
- se resolvió el Supplier por `provider_code`;
- snapshot = precio del `Product`, y descripción = título;
- subtotal = 2 × precio, total = subtotal + IVA, y todo `Decimal`;
- el folio sale `COT-…`.

Al final quedaron exactamente **2 `GET /productos` y 0 `POST /oauth/token`**, el `ProviderToken` sin cambios y los mismos recuentos de Category, Supplier, Product, Quote, QuoteItem y User que antes. No hubo errores ni 429.

## 5. Brechas encontradas (para el equipo)

Se registran como `test_caracterizacion_`: documentan cómo funciona hoy y no contradicen ningún contrato declarado.

| Brecha | Riesgo | Siguiente paso |
|---|---|---|
| **El precio en USD entra sin convertir** (I11) | Una cotización en pesos saldría con precios en dólares, tantas veces más barata como el tipo de cambio | Propuesta B en [19](19-propuesta-conversion-usd-mxn.md), para un plan aparte |
| **Los totales de `Quote` solo se leen en síncrono** (I12) | Las vistas son async (ASGI). Leer `quote.subtotal` directamente en una vista lanza `SynchronousOnlyOperation` | Leerlos con `sync_to_async` (como `totales()`) o añadir a `Quote` una variante async. Lo decide el equipo |
| **`Product.name` admite 200 caracteres** (I13) | El título real más largo observado mide 173 (§4.1). Uno de más de 200 haría fallar el alta con `DataError` | Recortar al crear el `Product` o ampliar el campo. Lo decide el motor de comparación o el equipo |
| **Precios con más de 2 decimales** (I14) | Se redondean al guardar (half-up, igual que `_money`). Antes de refrescar, el valor en memoria tiene 3 decimales. Hoy SYSCOM da como mucho 2 | Ninguno por ahora; si aparece otro proveedor con más decimales, cuantizar al crear el `Product` |
| **El Supplier "SYSCOM" no está enlazado en la BD de desarrollo** (I2, §4.1) | Sin el enlace, el motor no puede saber el distribuidor de cada oferta | Decisión pendiente del equipo (D10; [17 §6.3](17-adaptador-syscom.md)) |

## 6. Cómo ejecutarlas

```powershell
python manage.py test apps.quotes                                         # I1–I15
python manage.py test apps.quotes.tests.test_integracion_proveedores.CotizacionTests
python manage.py test                                                     # todo
```

No salen a la red ni ven credenciales reales. Las funciones y constantes de las muestras se importan de `apps/providers/tests/test_adaptador_syscom.py`, y la red de seguridad de `apps/providers/tests/soporte.py`. Nunca se importan clases de test, para que no se ejecuten dos veces.
