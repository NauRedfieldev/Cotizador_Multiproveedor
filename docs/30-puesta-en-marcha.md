# Puesta en marcha · Pendientes antes del uso real

> **Estado del documento: ABIERTO** · Última revisión: 2026-10-26 (B3 CCONOR hecho y probado; B6 esquema de roles hecho; M4 hecho. Pendientes: B3b correo de Torva, B5 Exel, B6b quién recibe cada rol y el Dueño, B7 aceptación y respaldos)
>
> Este es el único documento de pendientes; los demás de `docs/` son técnicos y no
> llevan estados. Reglas:
> 1. Actualizar la fila en el **mismo commit** que resuelve el pendiente.
> 2. Estados permitidos (solo §B y §C): `PENDIENTE`, `HECHO (fecha · commit/quién)`,
>    `NO APLICA (motivo)`.
> 3. Ante duda, gana la comprobación (§A "Cómo verificar"), no el texto.
> 4. Cuando todo §B y lo que §C marque como bloqueante esté `HECHO` o `NO APLICA`, cambiar este encabezado a
>    **«CERRADO el <fecha>: sin pendientes. Se conserva como registro.»**
>
> Nunca anotar contraseñas, contenido de `.env`/`.pgpass` ni datos bancarios.

## A. Preparar una instalación

Pasos que se repiten en **cada** equipo o servidor (desarrollo, compañero, producción).
No tienen estado global: que estén hechos en un equipo no significa que lo estén en otro.

| Paso | Qué hacer | Cómo verificar | Detalle |
| --- | --- | --- | --- |
| A1 | Respaldar BD + PDFs y probar la restauración | Prueba de restauración sin fallos de huella | [docs/29 §4–§5](29-respaldo-y-restauracion.md) |
| A2 | Aplicar migraciones de `quotes` (0003–0006), siempre después de A1 | `manage.py showmigrations quotes` sin `[ ]` | docs/25 §7.4, docs/29 §6 |
| A3 | Carpeta privada y persistente de PDFs | `QUOTE_PDF_ROOT` fuera de `static/`, permisos 700 e incluida en A1 | docs/24 §5, docs/29 §1 |
| A4 | Datos de la empresa: `CCONOR_EMPRESA_NAME` (obligatorio), `_RFC`, `_TAGLINE`, `_PHONE`, `_EMAIL` en `.env`; reiniciar | Crear un borrador no responde 503 | docs/28 §7 y §11 |
| A4b | Prefijo de folios nuevos de CCONOR: `CCONOR_EMPRESA_FOLIO_PREFIX` (2–6 letras; vacío = `COT`). Torva Telecom (opcional): `TORVA_EMPRESA_NAME`, `_RFC`, `_TAGLINE`, `_PHONE`, `_EMAIL`, `_FOLIO_PREFIX` (2–6 letras, distinto del de CCONOR y de `COT`) y `_LOGO=img/logo_torva.png` en `.env`; reiniciar | Aparece «Cotizar como» en el cotizador y cada folio nuevo sale con el prefijo de su empresa | docs/28 §14.16 |
| A4c | Correo por empresa: `CCONOR_SMTP_FROM`, `_HOST`, `_PORT`, `_SECURITY` (ssl/tls), `_USER`, `_PASSWORD` (y `TORVA_SMTP_*` cuando Torva tenga correo) en `.env`; reiniciar | Enviar una cotización emitida de prueba a un correo propio: llega al destinatario y su copia oculta al remitente. En macOS con Python de python.org, ejecutar antes «Install Certificates.command» (Aplicaciones › Python 3.x); sin eso el envío falla al verificar el certificado | docs/28 §14.17 |
| A4d | Correo de sistemas para invitaciones y contraseñas: `SISTEMAS_SMTP_FROM`, `_HOST`, `_PORT`, `_SECURITY`, `_USER`, `_PASSWORD` en `.env` (cuenta `sistemas@`); reiniciar | Invitar a un usuario de prueba con un correo propio: llega la invitación y el enlace deja elegir contraseña | docs/28 §14.19 |
| A5 | Crear grupos comerciales con `configure_commercial_groups` (superusuario, una vez tras A2; volver a ejecutarlo tras B6 para crear «Asesor CCONOR») | Existen «Personal CCONOR», «Asesor CCONOR» y «Administrador CCONOR» en el admin | docs/24 §8.5, docs/25 §7.3 |
| A6 | Asignar roles en «Usuarios y permisos» (menú de usuario) e invitar a cada persona; el Dueño (superusuario) se asigna solo en `/admin/` | Cada persona aparece con su rol; nadie de CCONOR con `is_staff` salvo el Dueño | docs/28 §14.19 |
| A7 | Base de IVA por proveedor en `CCONOR_PROVIDER_PRICE_BASES` (`settings/settings.py`, versionado): SYSCOM ya en `NET` (B4); Exel solo tras B5. Por instalación: credenciales `PROVIDER_<CODIGO>_*` en `.env` y alta del proveedor en el admin (docs/17 §7) | Agregar una oferta del proveedor al cotizador no responde 409 | docs/28 §13.3 |
| A7b | Tipo de cambio de SYSCOM: `SYSCOM_TIPO_CAMBIO_CAMPO` en `.env` (uno solo: `normal` = contado, `preferencial`, `un_dia`, `una_semana`, `dos_semanas`, `tres_semanas` o `un_mes`; decidido `normal`, M4); reiniciar | El chip del menú muestra «USD $… · SYSCOM» sin capturarlo; un valor mal escrito detiene el arranque | docs/28 §14.20 |
| A8 | `~/.pgpass` (permisos 600) para respaldar sin escribir la contraseña | `ls -l ~/.pgpass` → `-rw-------` | docs/29 §3 |
| A9 | Habilitar emisión: `COTIZACIONES_EMISION_HABILITADA=True` en `.env`, solo tras B7 | Botón «Emitir» activo en una revisión sin avisos pendientes | docs/25, docs/28 §7 |

## B. Decisiones y validaciones únicas

| # | Tema | Estado | Bloquea |
| --- | --- | --- | --- |
| B1 | Datos oficiales, vigencia, condiciones, firma del asesor y quién aprueba el formato del PDF (C15) | HECHO (2026-10-22 · usuario): datos oficiales de CCONOR y de Torva Telecom capturados en el `.env` de la instalación (docs/28 §14.16). Resuelto 2026-10-19: logo `static/img/logo.png` oficial; vigencia 15 días editable por el asesor; firma = nombre completo del perfil del asesor, **sin teléfono** (M1 no aplica); aprueba el formato el admin o un asesor autorizado; dirección de la empresa en `CCONOR_EMPRESA_TAGLINE` | Emitir a clientes reales |
| B2 | Si el cliente ve una leyenda de disponibilidad (detalle de C16) | HECHO (2026-10-19 · usuario; implementado con M3): leyenda fija en todas las cotizaciones comerciales, solo en el PDF (no en el correo), sin revelar stock por partida: «Precios y disponibilidad sujetos a confirmación al momento del pedido.» Avisos internos de stock HECHOS 2026-10-18 · d3cbedc | Texto del PDF/correo |
| B3 | Remitente, canal y confirmación del correo de CCONOR (C17) | HECHO (2026-10-23 · usuario + prueba real): remitente `ventas1@cconorsoluciones.mx`, canal = SMTP del hosting del dominio (`CCONOR_SMTP_*` en `.env`), confirmación = copia oculta completa (con PDF) al remitente. Prueba real: cotización emitida `PRU-2026-0001` enviada; llegaron el correo y su copia. En macOS se requirió «Install Certificates.command» (A4c). Detalle: docs/28 §14.17 | Correo real |
| B3b | Correo propio de Torva Telecom (`TORVA_SMTP_*`) | PENDIENTE (2026-10-23 · usuario: se define después). Mientras tanto los envíos de Torva solo se escriben en la consola | Correo real de Torva |
| B4 | SYSCOM: confirmar si sus precios vienen con o sin IVA | HECHO (2026-10-19 · usuario + verificación): `NET` activado en `CCONOR_PROVIDER_PRICE_BASES`. Evidencia: documentación ([developers.syscom.mx](https://developers.syscom.mx/docs/productos): `iva=1` → con IVA ×1.16) y cotejo con el portal de la cuenta de CCONOR: `precio_lista` de la API con `iva=0` es idéntico al del portal en «Sin IVA», y en «Con IVA» el portal muestra exactamente ×1.16. Precio de cuenta: con `financiero=true` la API da exactamente el precio del portal (~4% menos, descuento por forma de pago); decisión: costo **sin** descuento financiero (no se envía `financiero`), el seguro si CCONOR paga con tarjeta o a crédito | Ofertas SYSCOM en el cotizador comercial |
| B5 | Exel del Norte: acceso real a la API y base de IVA | PENDIENTE: acceso a la API pendiente (2026-10-19) | Ofertas Exel en el cotizador comercial |
| B6 | Esquema de roles y su administración | HECHO (2026-10-23/26 · usuario, escenario D; rama `feature/correo-prueba-y-roles`): tres roles fijos (Personal, Asesor, Administrador) + permisos extra como etiqueta; Dueño = superusuario intocable desde el panel; los administradores pueden nombrar administradores; altas por invitación desde `sistemas@` y recuperar contraseña por enlace (docs/28 §14.19); el tipo de cambio de SYSCOM es automático para todos y solo Asesor/Administrador lo cambian (docs/28 §14.20) | A6 |
| B6b | Quién recibe cada rol (anotar por puesto, no por nombre) y quién será el Dueño (superusuario definitivo) | PENDIENTE: lo decide CCONOR; luego se invita a cada persona (A6) y se pasa el superusuario en `/admin/` | A6 |
| B7 | Aceptación, retención, frecuencia y responsable de respaldos (C18) | PENDIENTE: sin definir (2026-10-19); recomendaciones en docs/29 §7. Aceptación sugerida: un administrador hace 2–3 cotizaciones de prueba completas y da el visto bueno antes de A9 | Uso real y A9 |

## C. Mejoras acordadas (no bloquean el uso real salvo que se indique)

| # | Mejora | Estado | Origen |
| --- | --- | --- | --- |
| M1 | Teléfono del asesor en el PDF y el correo | NO APLICA (2026-10-22 · CCONOR: no les resulta funcional) | B1 |
| M2 | Condiciones comerciales precargadas y editables en cada cotización nueva | HECHO (2026-10-19 · admin «Condiciones predeterminadas», migración 0006): un administrador las agrega, ordena o desactiva; el asesor las edita en cada cotización; las cotizaciones guardadas conservan las suyas. Lista inicial vacía: los textos oficiales los captura CCONOR (ver B1). Uso: docs/28 §14.4 | B1 |
| M3 | Leyenda fija de disponibilidad en el PDF comercial (no en el correo); aplica solo a emisiones nuevas, las emitidas no cambian | HECHO (2026-10-19 · `LEYENDA_DISPONIBILIDAD` en `apps/quotes/pdf.py`) | B2 |
| M4 | Tipo de cambio de SYSCOM del día (`GET /tipocambio`, público) | HECHO (2026-10-26 · usuario): se aplica solo para todos los roles con el valor `normal` (contado) de `SYSCOM_TIPO_CAMBIO_CAMPO`; solo Asesor/Administrador (o el extra) lo cambian; si SYSCOM falla se usa el último valor del día. Detalle: docs/28 §14.20 | B4 |

## D. Orden recomendado

A1 → A2 → A3 → A4 → A4c/A4d → A5 → (B6b) A6 → (B4/B5) A7 → A7b → aceptación (B7) → A9.
