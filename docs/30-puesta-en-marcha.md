# Puesta en marcha · Pendientes antes del uso real

> **Estado del documento: ABIERTO** · Última revisión: 2026-10-19 (B1 parcial, B2 hecho, B4 y B5 anotados)
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
| A2 | Aplicar migraciones de `quotes` (0003–0005), siempre después de A1 | `manage.py showmigrations quotes` sin `[ ]` | docs/25 §7.4, docs/29 §6 |
| A3 | Carpeta privada y persistente de PDFs | `QUOTE_PDF_ROOT` fuera de `static/`, permisos 700 e incluida en A1 | docs/24 §5, docs/29 §1 |
| A4 | Datos de la empresa: `CCONOR_EMPRESA_NAME` (obligatorio), `_RFC`, `_TAGLINE`, `_PHONE`, `_EMAIL` en `.env`; reiniciar | Crear un borrador no responde 503 | docs/28 §7 y §11 |
| A5 | Crear grupos comerciales con `configure_commercial_groups` (superusuario, una vez tras A2) | Existen «Personal CCONOR» y «Administrador CCONOR» en el admin | docs/24 §8.5, docs/25 §7.3 |
| A6 | Asignar usuarios a esos grupos según B6 | Admin → usuario → grupos | docs/24 §8.5 |
| A7 | Base de IVA por proveedor en `CCONOR_PROVIDER_PRICE_BASES` (`settings/settings.py`), solo con B4/B5 resueltos | Agregar una oferta del proveedor al cotizador no responde 409 | docs/28 §13.3 |
| A8 | `~/.pgpass` (permisos 600) para respaldar sin escribir la contraseña | `ls -l ~/.pgpass` → `-rw-------` | docs/29 §3 |
| A9 | Habilitar emisión: `COTIZACIONES_EMISION_HABILITADA=True` en `.env`, solo tras B7 | Botón «Emitir» activo en una revisión sin avisos pendientes | docs/25, docs/28 §7 |

## B. Decisiones y validaciones únicas

| # | Tema | Estado | Bloquea |
| --- | --- | --- | --- |
| B1 | Datos oficiales, vigencia, condiciones, firma del asesor y quién aprueba el formato del PDF (C15) | PENDIENTE: datos oficiales de la empresa (nombre y dirección provisionales en uso) y teléfono del asesor (requiere desarrollo, M1). Resuelto 2026-10-19: logo `static/img/logo.png` oficial; vigencia 15 días editable por el asesor; firma = nombre completo del perfil + teléfono del asesor; aprueba el formato el admin o un asesor autorizado; dirección de la empresa va en `CCONOR_EMPRESA_TAGLINE` | Emitir a clientes reales |
| B2 | Si el cliente ve una leyenda de disponibilidad (detalle de C16) | HECHO (2026-10-19 · usuario; implementado con M3): leyenda fija en todas las cotizaciones comerciales, solo en el PDF (no en el correo), sin revelar stock por partida: «Precios y disponibilidad sujetos a confirmación al momento del pedido.» Avisos internos de stock HECHOS 2026-10-18 · d3cbedc | Texto del PDF/correo |
| B3 | Remitente, canal y confirmación del correo (C17) | PENDIENTE | Correo real |
| B4 | SYSCOM: confirmar si sus precios vienen con o sin IVA | PENDIENTE: evidencia documental 2026-10-19 ([developers.syscom.mx](https://developers.syscom.mx/docs/productos): `iva=1` → precios con IVA ×1.16; el adaptador envía `iva=0`, lo que apunta a `NET`). Falta cotejar un producto real contra factura o portal de CCONOR. Mientras tanto, las ofertas SYSCOM se rechazan en el cotizador comercial (409), sin riesgo de doble IVA | Ofertas SYSCOM en el cotizador comercial |
| B5 | Exel del Norte: acceso real a la API y base de IVA | PENDIENTE: acceso a la API pendiente (2026-10-19) | Ofertas Exel en el cotizador comercial |
| B6 | Quién recibe cada rol (Personal / Administrador CCONOR) | PENDIENTE | A6 |
| B7 | Aceptación, retención, frecuencia y responsable de respaldos (C18) | PENDIENTE (recomendaciones en docs/29 §7) | Uso real y A9 |

## C. Mejoras acordadas (no bloquean el uso real salvo que se indique)

| # | Mejora | Estado | Origen |
| --- | --- | --- | --- |
| M1 | Teléfono del asesor en el PDF y el correo (hoy el perfil de usuario no tiene teléfono; requiere diseño y migración) | PENDIENTE · bloquea cerrar B1 | B1 |
| M2 | Condiciones comerciales precargadas y editables en cada cotización nueva | PENDIENTE · no bloquea | B1 |
| M3 | Leyenda fija de disponibilidad en el PDF comercial (no en el correo); aplica solo a emisiones nuevas, las emitidas no cambian | HECHO (2026-10-19 · `LEYENDA_DISPONIBILIDAD` en `apps/quotes/pdf.py`) | B2 |

## D. Orden recomendado

A1 → A2 → A3 → A4 → A5 → (B6) A6 → (B4/B5) A7 → aceptación (B7) → A9.
