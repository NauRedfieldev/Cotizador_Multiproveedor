# Respaldo y restauración · BD PostgreSQL + PDFs originales

Guía técnica para copiar y recuperar los datos del multi-cotizador. Complementa el
procedimiento operativo de [docs/24 §5](24-sdd-m8-snapshot-pdf.md) (originales PDF,
huérfanos y emisiones inciertas). No contiene contraseñas ni datos reales.

## 1. Qué se respalda y por qué juntos

| Pieza | Dónde vive | Qué contiene |
| --- | --- | --- |
| Base de datos PostgreSQL | `NAME_DB` en `HOST_DB:PORT_DB` (`.env`) | Cotizaciones, revisiones, partidas, confirmaciones, huella SHA-256 de cada PDF emitido |
| PDFs originales | `QUOTE_PDF_ROOT` (por defecto `private_quote_files/`, ignorado por Git) | Bytes exactos de cada revisión emitida (`quotes/<quote>/r<n>/<uuid>.pdf`) |
| `.env` | Raíz del repo, privado | Credenciales y configuración; **no** va en la copia compartida ni en Git |

- La BD sin la carpeta pierde los originales; la carpeta sin la BD pierde a qué
  cotización pertenece cada PDF y su huella. Siempre respaldar **las dos juntas**.
- `.env` se guarda aparte, en un lugar seguro (gestor de contraseñas o medio cifrado).
- Un PDF original nunca se regenera para sustituir uno perdido (docs/24 §5): solo se
  restauran sus bytes exactos desde un respaldo.

## 2. Orden consistente

1. **Primero la BD** (`pg_dump`, instantánea consistente).
2. **Después la carpeta de PDFs.**

Los originales emitidos nunca se borran, así que cualquier emisión que exista en el
dump ya tiene su archivo cuando se copia la carpeta. Lo inverso (PDFs primero) puede
dejar una emisión en la BD sin su archivo. Un PDF de más en la copia es un huérfano
inofensivo (docs/24 §5, paso 2). No respaldar a mitad de una migración.

## 3. Preparación (una vez)

Contraseña sin escribirla en comandos: archivo `.pgpass` con permisos `600`
(`host:puerto:base:usuario:contraseña`, una línea por conexión).

```bash
# macOS
touch ~/.pgpass && chmod 600 ~/.pgpass   # editar y añadir: 127.0.0.1:5433:*:<USER_DB>:<PASSWORD_DB>
mkdir -p "$HOME/Library/Application Support/CCONOR/backups"
chmod 700 "$HOME/Library/Application Support/CCONOR/backups"
```

Windows: `%APPDATA%\postgresql\pgpass.conf` con el mismo formato; carpeta de respaldos
fuera del repo con acceso solo del usuario.

## 4. Respaldar (macOS)

```bash
PGBIN=/Library/PostgreSQL/18/bin
DB=<NAME_DB>; DBUSER=<USER_DB>; HOST=127.0.0.1; PORT=5433
PDF_ROOT=<QUOTE_PDF_ROOT>            # por defecto <repo>/private_quote_files
OUT="$HOME/Library/Application Support/CCONOR/backups"
STAMP=$(date +%Y%m%d-%H%M%S)

# 1) BD (formato custom, comprimido)
"$PGBIN/pg_dump" -h $HOST -p $PORT -U $DBUSER -d $DB -Fc -f "$OUT/cconor-db-$STAMP.dump"
# 2) PDFs (si la carpeta aún no existe, no hay originales que copiar)
[ -d "$PDF_ROOT" ] && tar -C "$(dirname "$PDF_ROOT")" -czf "$OUT/cconor-pdf-$STAMP.tar.gz" "$(basename "$PDF_ROOT")"
# 3) Huellas y permisos
cd "$OUT" && shasum -a 256 cconor-*-$STAMP.* > "cconor-$STAMP.sha256" && chmod 600 cconor-*$STAMP*
# 4) Comprobación rápida: el dump se puede leer
"$PGBIN/pg_restore" --list "$OUT/cconor-db-$STAMP.dump" | head
```

Windows (PowerShell), mismos pasos:

```powershell
$PGBIN = "C:\Program Files\PostgreSQL\18\bin"; $STAMP = Get-Date -Format "yyyyMMdd-HHmmss"
& "$PGBIN\pg_dump.exe" -h 127.0.0.1 -p 5433 -U <USER_DB> -d <NAME_DB> -Fc -f "$OUT\cconor-db-$STAMP.dump"
Compress-Archive -Path <QUOTE_PDF_ROOT> -DestinationPath "$OUT\cconor-pdf-$STAMP.zip"
Get-FileHash "$OUT\cconor-*-$STAMP.*" -Algorithm SHA256 | Out-File "$OUT\cconor-$STAMP.sha256"
```

## 5. Probar que la copia sirve (sin tocar la BD de trabajo)

Un respaldo solo cuenta si se restauró al menos una vez. Restaurar en una BD **nueva**:

```bash
TEST=cconor_restore_test_$STAMP
shasum -a 256 -c "$OUT/cconor-$STAMP.sha256"
"$PGBIN/createdb" -h $HOST -p $PORT -U $DBUSER $TEST
"$PGBIN/pg_restore" -h $HOST -p $PORT -U $DBUSER -d $TEST --no-owner "$OUT/cconor-db-$STAMP.dump"
for t in quotes_quote quotes_quoterevision quotes_quotesnapshot; do
  "$PGBIN/psql" -h $HOST -p $PORT -U $DBUSER -d $TEST -tAc "SELECT '$t', count(*) FROM $t"
done   # comparar con los mismos conteos en la BD original
```

Verificar cada PDF emitido contra su huella con el servicio existente
`leer_pdf_emitido` (`apps/quotes/snapshots.py`). Las variables de entorno tienen
prioridad sobre `.env`, así que se apunta a la copia sin editarlo:

```bash
RESTORE_PDF=$(mktemp -d) && tar -C "$RESTORE_PDF" -xzf "$OUT/cconor-pdf-$STAMP.tar.gz"
NAME_DB=$TEST QUOTE_PDF_ROOT="$RESTORE_PDF/$(basename "$PDF_ROOT")" venv/bin/python manage.py shell -c "
from apps.quotes.models import QuoteRevision
from apps.quotes.snapshots import leer_pdf_emitido
ok, fallos = 0, []
for r in QuoteRevision.objects.filter(status='ISSUED').exclude(pdf=''):
    try: leer_pdf_emitido(r); ok += 1
    except Exception as e: fallos.append((r.pk, str(e)))
print('PDF OK:', ok, '| fallos:', fallos)"
```

Al terminar: `"$PGBIN/dropdb" -h $HOST -p $PORT -U $DBUSER $TEST` y borrar `$RESTORE_PDF`.
Si hay fallos, el respaldo no es válido: repetir antes de depender de él.

## 6. Restaurar de verdad (pérdida o rollback)

1. Detener la aplicación (sin escrituras durante la restauración).
2. Respaldar el estado actual aunque esté dañado (§4), para no perder evidencia.
3. BD: `pg_restore -h … -U … -d <NAME_DB> --clean --if-exists "<dump>"`.
4. PDFs: extraer el archivo en `QUOTE_PDF_ROOT` conservando rutas y permisos `600/700`.
5. Verificar conteos y huellas (§5) **antes** de reabrir la aplicación.

**Rollback de una migración** = restaurar el respaldo tomado justo antes de migrar.
Solo es seguro mientras no se hayan emitido cotizaciones después de migrar; con
emisiones nuevas no se revierte (docs/24 §5): se corrige hacia delante.

## 7. Política operativa · recomendaciones (C18)

Recomendaciones técnicas. Su aprobación se sigue en [docs/30 B7](30-puesta-en-marcha.md):

| Tema | Recomendación |
| --- | --- |
| Frecuencia | Respaldo diario de BD + PDFs, y siempre antes de migrar o actualizar |
| Copia externa | Al menos una copia fuera del equipo (disco externo o nube con acceso restringido) |
| Retención | ~30 días de respaldos diarios; conservación de cotizaciones ≥ 5 años, sin borrar historial |
| Prueba | Restauración completa (§5) al menos cada 3 meses |
| Responsable | Una persona fija y un suplente |
| Original perdido sin respaldo | Registrar la pérdida; si hace falta, nueva revisión. Nunca regenerar como original |
