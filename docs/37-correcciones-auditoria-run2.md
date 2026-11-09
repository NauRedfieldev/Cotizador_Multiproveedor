# 37 · Correcciones de la auditoría de seguridad run-2

**Fecha:** 2026-11-09 · **Rama:** `feature/correccion-de-errores` (desde `main` 1b9773c)
**Relacionado:**
- auditoría run-1: `docs/run-1/`;
- auditoría run-2: informe fuera del repo (`audit-env/out/run-2/REPORT.md`);
- [13 · SDD M2](13-sdd-m2-contrato-y-orquestacion.md);
- [25 · M8](25-m8-snapshot-pdf-integracion.md);
- [20 · Integrar API de proveedor](20-guia-integrar-api-de-proveedor.md);
- [33 · Proveedor por web scraping](33-guia-integrar-proveedor-por-web-scraping.md).

> Número 37: las ramas de Docker (`feature/docker*`) ya usan el 35 y el 36. Este documento no sustituye a ninguno
> anterior: explica qué cambió y por qué.

## 1. Origen
- La run-2 auditó `a9e64dd` con pruebas dinámicas en un contenedor aislado, contra mocks de SYSCOM, Exel y SMTP.
- `main` ya había corregido los hallazgos de la run-1 (rama `feature/seguridad-acceso-cuentas`), pero los de la
  run-2 seguían abiertos.
- Este documento recoge cómo se cerró cada uno.

| Hallazgo | Riesgo | Corrección |
| --- | --- | --- |
| #5 (run-1) | Un staff con `change_provider` cambiaba `base_url` y recibía el token, el `client_secret` de SYSCOM y la contraseña de Exel | §2 |
| #19 | El PDF v1 mostraba costos de proveedor a quien no tenía `view_commercial_costs` (dueño con el rol revocado, staff sin rol) | §3 |
| #17 | Una cotización ajena daba 403 y una inexistente 404: se podían contar los folios | §3 |
| #16 | El correo de cotizaciones no tenía límite | §4 |
| #20 | Un PDF de borrador pesado ocupaba el único worker (36–69 s por PDF) | §4 |
| #21 | Con `DEBUG=True`, el PDF demo era anónimo y un folio largo daba un 500 técnico | §5 |
| #18 | Las respuestas de los proveedores se leían enteras en memoria (60 MB pasaron) | §6 |
| Punto 9 | Con `DEBUG=True`, la página de error del correo mostraba el mensaje del cliente | §4 |

## 2. `Provider.base_url` (#5)
- **Dónde:** `apps/providers/admin.py`, `apps/providers/models.py` y `settings/settings.py`.
- **Solo el superusuario** da de alta proveedores o cambia `base_url`. Para el resto del staff, el campo es de
  solo lectura y la página de alta da 403.
- **`ProviderForm.clean_base_url` → `validar_base_url(code, url)`** (`models.py`). Exige:
  - `https`;
  - sin `usuario:clave@`;
  - sin puerto;
  - un host declarado en `CCONOR_PROVIDER_HOSTS[code]`.
- **Por qué en el formulario y no en `Provider.clean()`:**
  - Solo valida cuando el campo es editable (superusuario).
  - En `clean()` rompía con un 500 el guardado del staff, para quien `base_url` es de solo lectura, si el valor
    guardado no estaba en la lista. Lo detectó la verificación con la auditoría.
  - `Provider.objects.create()` no valida, así que las pruebas con hosts ficticios siguen igual.
- **Al cambiar `base_url` se borra el `ProviderToken`:** el token del host anterior no viaja al nuevo.

```python
# settings/settings.py
CCONOR_PROVIDER_HOSTS = {
    'syscom': ('developers.syscom.mx',),
    'exel': ('www.exel.com.mx',),
}
```

**Para un proveedor nuevo** (guías de docs/20 y docs/33): añadir su `code` con el host oficial confirmado. Sin él,
el admin no deja guardar su `base_url`.

## 3. Permiso de costos en documentos y folios ajenos (#19 y #17)
**Dónde:** `apps/quotes/snapshots.py`, `api.py` y `views.py`.

**Regla de documentos**
- `obtener_documento` exige `view_commercial_costs` para **cualquier** versión (v1, v2 o histórica), igual que la
  API.
- Antes solo comprobaba ser dueño o staff para v1, y el PDF v1 lleva el costo de origen, el proveedor y el stock.
- El staff conserva el salto de dueño (diseño de la run-1), pero necesita la capacidad comercial.

**Orden de los chequeos: capacidad antes de buscar el folio**
- `obtener_documento` llama a `require_commercial_permission` antes de `Quote.objects.get(folio=…)`.
- Las rutas de la API con `<folio>` lo hacen en el decorador `_api`.
- Así, quien no tiene la capacidad recibe **403 exista o no** la cotización.

**Folio ajeno**
- `authorize()` lanza `CotizacionAjena`, que es subclase de `PermissionDenied`.
- Dentro del código no cambia nada: los `except PermissionDenied` de confirmaciones siguen igual.
- Hacia fuera, `_respuesta_de_error` (API) y `_document_error` (PDF y correo) la responden con **404 y el mismo
  cuerpo** que un folio inexistente.

| Quién | Folio propio | Folio ajeno | Folio inexistente |
| --- | --- | --- | --- |
| Con rol | 200 | **404** (antes 403) | 404 |
| Staff con rol | 200 | 200 (diseño) | 404 |
| Sin rol (incluido staff sin rol) | 403 | 403 | **403** (antes 404) |

## 4. Límites y correo (#16, #20 y punto 9)
**Dónde:** `apps/quotes/views.py`.

**Límites por usuario**
- Usan la caché `limites` (la misma de login y recuperación, vía `usuarios._sumar`).
- Responden **429** con un JSON de error.

| Límite | Valor | Ventana | Constante |
| --- | --- | --- | --- |
| Descargas de PDF | 10 | 1 minuto | `LIMITE_PDF_POR_MINUTO` |
| Correos de cotización | 20 | 1 hora | `LIMITE_CORREOS_POR_HORA` |

- Viven en la memoria de cada proceso. Con un solo worker (docs/35 de la rama Docker) el límite es exacto; con
  varios, habría que pasar la caché `limites` a un almacén compartido.

**Correo**
- Un asunto con `\r` o `\n` → **400**. Antes daba 500, aunque no se enviaba nada.
- Armar y enviar el correo va dentro de `try`: un fallo responde JSON 500 y queda en el log. Con `DEBUG=True`,
  Django no censura la página técnica, que mostraba el mensaje privado del cliente.
- `@sensitive_post_parameters()` y `@sensitive_variables()` protegen además los informes de error con
  `DEBUG=False`.

## 5. PDF demo (#21)
- `demo/cotizaciones/<folio>/pdf/` exige sesión (`login_required`).
- Responde 404 si no hay `DEBUG`, si el usuario no es staff o si el folio tiene más de 40 caracteres.

## 6. Tope de respuesta de proveedores (#18)
**Dónde:** `apps/providers/service.py` y `apps/quotes/views.py`.
- `TransporteConTope` envuelve el transporte httpx: el real o el `MockTransport` de las pruebas.
- Corta en `MAX_BYTES_RESPUESTA` (5 MB). Sirve tanto si `Content-Length` lo anuncia como si el cuerpo llega por
  trozos.
- Lanza `RespuestaDemasiadoGrande` (un `httpx.HTTPError`).
- `search_all` lo traduce a `ProviderResponseError` («Respuesta demasiado grande»): el proveedor aparece caído en el
  resultado y nunca da un 500 (contrato de M2).
- El tipo de cambio de SYSCOM usa el mismo transporte. Allí cae en su `except httpx.HTTPError` y aplica el respaldo
  del día.
- Los adaptadores no cambian.

## 7. Pruebas
- `apps/providers/tests/test_correcciones_run2.py`: validador, admin, token borrado y tope (con y sin
  `Content-Length`).
- `apps/quotes/tests/test_correcciones_run2.py`:
  - PDF v1 con y sin capacidad;
  - matriz ajeno frente a inexistente para 3 principales en 11 rutas;
  - límites 429, asunto, `DEBUG` y demo.
- Pruebas existentes ajustadas a propósito (403 → 404 para otro asesor con rol; el staff necesita la capacidad
  comercial para leer documentos):
  - `test_api_cotizaciones.py`;
  - `test_snapshots.py`.
- Para volver a pasar la auditoría contra el código corregido: `git archive` del commit a `audit-env/src/<commit>` y
  `AUDIT_SRC=… ./audit-run.sh …` (ver el `REPORT.md` de la run-2, §9).
