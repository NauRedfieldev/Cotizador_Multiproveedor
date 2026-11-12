# 37 · Correcciones de la auditoría de seguridad run-2

**Fecha:** 2026-11-09 · **Rama:** `feature/correccion-de-errores` (desde `main` 1b9773c)
**Relacionado:**
- auditoría run-1: `docs/run-1/`;
- auditoría run-2: informe fuera del repo (`audit-env/out/run-2/REPORT.md`);
- [13 · SDD M2](13-sdd-m2-contrato-y-orquestacion.md);
- [25 · M8](25-m8-snapshot-pdf-integracion.md);
- [20 · Integrar API de proveedor](20-guia-integrar-api-de-proveedor.md);
- [33 · Proveedor por web scraping](33-guia-integrar-proveedor-por-web-scraping.md).

> Este documento no sustituye a ninguno anterior: explica qué cambió y por qué.

## 1. Origen
- La run-2 auditó `a9e64dd` con pruebas dinámicas en un entorno aislado, contra mocks de SYSCOM, Exel y SMTP.
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

- Viven en la memoria de cada proceso. Con un solo worker (`uvicorn --workers 1`) el límite es exacto; con
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

## 8. Endurecimiento de la run-1 (§5 del informe) · 2026-11-12
Rama `feature/endurecimiento-seguridad`. No eran vulnerabilidades por sí solas; se aplicaron cinco de la lista.

| Cambio | Dónde | Por qué |
| --- | --- | --- |
| La sesión vence tras **8 h sin usar el sistema** (`SESSION_COOKIE_AGE`) y cada visita la renueva (`SESSION_SAVE_EVERY_REQUEST`) | `settings/settings.py` | Antes duraba 2 semanas aunque nadie la usara. Sin renovar en cada visita, las 8 h contarían desde el último cambio guardado en la sesión y podían cortar a alguien a media cotización |
| Borrar las sesiones vencidas a diario con `clearsessions` | docs/30 A14 | Vencer no borra la fila de `django_session` |
| **PostgreSQL corta cualquier consulta de más de 30 s** (`statement_timeout`; `STATEMENT_TIMEOUT_MS` en `.env` para cambiarlo) | `settings/settings.py` | Una consulta atorada no deja ocupada la conexión ni el único worker. No afecta a `pg_dump`; si una migración pesada se corta, subirlo solo para ella |
| **Usuarios y grupos en `/admin/` solo para el superusuario** (`SoloSuperusuario`) | `apps/quotes/admin.py` | Quien edita usuarios en `/admin/` puede darse superusuario. Un staff con permisos de `auth` ahora recibe 403. Los roles se gestionan en «Usuarios y permisos» |
| **SRI en `bootstrap-icons`** (`integrity` + `crossorigin`) | `templates/base.html` | Si el CDN sirve otro archivo, el navegador no lo carga. Bootstrap ya lo tenía. Google Fonts no admite SRI (su CSS cambia según el navegador) |
| `.gitignore`: `.env.*`, `*.pem`, `*.key` | `.gitignore` | Prevención: el historial no tiene ninguno de esos archivos. `.env_example` sí se sube |

**Al actualizar el SRI:** si se cambia la versión de un recurso del CDN, recalcular su hash
(`curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A`); con el hash viejo el navegador no lo carga.

**Riesgo conocido de guardar en cada visita:** cada petición reescribe la sesión. Si una petición rápida empieza
antes y termina después de otra que cambia la sesión (por ejemplo, una búsqueda), puede reescribir la versión
anterior. La ventana es de milisegundos y las búsquedas largas ya escriben la sesión de por sí.

**Pruebas:** `settings/tests.py` (`TiempoMaximoDeConsultaTests`, `TiempoMaximoEnLaConexionTests`),
`apps/quotes/tests/test_usuarios.py` (`test_usuarios_y_grupos_en_admin_solo_para_el_superusuario`,
`VencimientoDeSesionTests`) y `apps/quotes/tests/test_vistas_cotizacion.py` (`RecursosExternosTests`).
Ajuste a propósito: las tres pruebas de «la vista previa no escribe» de `test_api_cotizaciones.py` usan
`escribe_datos()`, que ignora el `UPDATE` de `django_session` (ahora cada visita guarda la sesión) y sigue
detectando cualquier escritura en las tablas de cotizaciones.

Quedan sin aplicar del §5: sacar `_cerrar_sesiones` de la transacción, que el token de recuperación cubra
`is_staff`/`is_superuser`, `LogEntry` en `set_commercial_permission`, revisar `MANAGE` antes del lock, no contar
invitaciones pendientes como administrador, mover `_correo_sistemas()`, rechazar caracteres de control en nombres
y no escribir enlaces con token en el correo de consola.

## 9. Pendientes de código cerrados · 2026-11-12
Rama `fix/pendientes-auditoria`. Cierra el #20 (que en §4 quedó a medias), el admin de catálogo y la lista de §8.

### 9.1 PDF de borrador (#20)
**Dónde:** `apps/quotes/snapshots.py` (`_pdf_de_borrador`), `snapshot_values.py`, `views.py` y la caché `pdfs` de
`settings/settings.py`.

| Medida | Cómo | Por qué |
| --- | --- | --- |
| **Caché del PDF ya generado** | Caché `pdfs` (LocMem, 20 entradas, 10 min). La clave es el `sha256` del contexto completo más los ids de la cotización y la revisión | Un borrador que no cambió se entrega al instante. Cualquier cambio (partidas, tipo de cambio, cabecera) da otra clave, así que nunca hay que invalidar. Va aparte de `default` para no desalojar el tipo de cambio ni las ofertas |
| **Máximo 2 PDF de borrador a la vez** (`MAX_PDF_SIMULTANEOS`) | `threading.BoundedSemaphore`; si no hay hueco, `PdfOcupado` → **503** con `Retry-After: 5` | Con un solo worker (docs/30 A12) es exacto. Lo que sale de la caché no ocupa hueco |
| **Tope de texto: 150 000 caracteres por revisión** (`MAX_TEXTO_TOTAL`) | `sumar_texto` suma `name`, `edited_name` y `description` de las partidas al guardar (v1 y v2); al pasarlo, `ValidationError` | El tiempo del PDF crece con el texto: 500 partidas de 6000 caracteres tardaban 69 s. Con el tope, medido en local: el peor caso (25 partidas de 6000) tarda 3,3 s; 250 partidas de 600, 1,7 s; desde la caché, 0,003 s |

- La caché se consulta después de `require_commercial_permission` y `authorize`: el PDF solo lo recibe quien
  ya podía generarlo.
- La emisión no cambia: genera el PDF una sola vez y lo guarda en disco.
- Las revisiones guardadas antes del tope no se tocan; las protegen la caché y el semáforo.

### 9.2 `/admin/` solo para el Dueño
- `apps/quotes/admin.py`: `admin.site.has_permission = _solo_dueno` (activo, staff **y** superusuario).
- Cubre catálogo (`SinonimoRed` y `ProductoCanonico`, que deciden cómo se emparejan los productos de todos),
  proveedores, cotizaciones y cualquier modelo futuro.
- Un staff que no sea superusuario va al login del admin (302).
- Ningún rol del portal recibe `is_staff`, así que en el uso normal no cambia nada.
- Segunda capa, se queda igual: `SoloSuperusuario` en usuarios y grupos, y `base_url` de solo lectura.
- `admin/password_change/` exige seguir siendo el Dueño: quitarle staff **o** superusuario a la mitad gana
  (`exige_dueno`, antes `exige_staff`).

### 9.3 Las 8 recomendaciones de §8
**Dónde:** `apps/quotes/usuarios.py`, `permissions.py`, `admin.py`, `templates/registration/correo_acceso.*` y
`static/js/usuarios.js`.

| Recomendación | Cambio |
| --- | --- |
| Sacar `_cerrar_sesiones` de la transacción | `transaction.on_commit` en el portal y en `/admin/`: recorrer las sesiones ya no retiene el lock del grupo. Mientras tanto no hay hueco, porque Django no autentica a un usuario inactivo |
| Que el enlace de contraseña cubra `is_active`, `is_staff` e `is_superuser` | `_GeneradorDeEnlaces._make_hash_value` los añade a la huella de Django |
| `LogEntry` en `set_commercial_permission` | Con el prefijo `B6: ` del historial de «Usuarios y permisos». La ruta `api/v1/commercial/permissions/` se conserva |
| Revisar `MANAGE` antes del lock | `_bloquear_y_cargar` lo revisa antes del `select_for_update` del grupo y otra vez con el lock tomado |
| No contar una invitación pendiente como administrador | `_hay_otro_administrador` excluye a quien nunca entró y tiene contraseña inutilizable (criterio de `_pendiente`) |
| `_correo_sistemas()` después del permiso | En `invitar`, `reenviar_invitacion` y `enviar_enlace`: quien no gestiona usuarios recibe 403, no un 503 que delata la configuración del correo |
| Rechazar caracteres de control en nombres | `_texto` rechaza las categorías Unicode `Cc` y `Cf`: saltos de línea, ancho cero e inversiones de dirección |
| La consola no escribe enlaces con token | Si el correo de «sistemas» es el de consola (falta `SISTEMAS_SMTP_HOST`), el correo sale **sin el enlace** y con un aviso. El panel lo marca como no enviado. Las pruebas usan locmem y sí lo reciben |

### 9.4 Cambios de comportamiento para el equipo
- **PDF de borrador:** 503 «Se están generando otros PDF» si ya hay 2 en curso.
- **Guardar un borrador** con más de 150 000 caracteres de texto en las partidas: 400.
- **`/admin/`:** solo el Dueño entra.
- **Enlaces de contraseña** enviados antes de desplegar esta versión: dejan de servir una vez (cambió la huella).
  Basta con reenviarlos.
- **Sin `SISTEMAS_SMTP_HOST`:** las invitaciones ya no se pueden completar desde la consola; hay que configurar el
  correo de sistemas.

### 9.5 Pruebas
- `apps/quotes/tests/test_correcciones_run2.py` (`PdfDeBorradorTests`): caché, borrador cambiado, 503 con el
  semáforo lleno y tope v1/v2.
- `apps/quotes/tests/test_usuarios.py`:
  - `test_todo_el_admin_solo_para_el_dueno`;
  - `EndurecimientoRun1Tests`, una prueba por recomendación (`test_a_…` a `test_h_…`).
- Ajustadas a propósito, porque el staff ya no entra a `/admin/`:
  - `apps/providers/tests/test_correcciones_run2.py`: la regresión de `base_url` se prueba sobre el `ModelAdmin`;
  - `test_usuarios.py`: 302 en lugar de 403, el cambio de contraseña en admin con el Dueño, y las sesiones se
    cierran con `captureOnCommitCallbacks`.
