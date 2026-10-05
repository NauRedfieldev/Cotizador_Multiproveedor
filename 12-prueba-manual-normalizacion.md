# Prueba manual de normalización M5a

## Para qué sirve

`tools/probar_normalizacion.py` permite escribir ejemplos en terminal y ver cómo los transforma M5a. Usa las funciones reales de `apps/catalog/normalizer.py`; no duplica sus reglas ni modifica el cotizador.

**No necesita `.env`, Django ni PostgreSQL.** Solo usa Python y la biblioteca estándar. No guarda entradas, crea sinónimos, consulta APIs ni realiza matching. El texto se muestra en la terminal: evita introducir datos sensibles.

## Cómo iniciar

Desde la raíz del repositorio, en macOS:

```bash
source venv/bin/activate
python tools/probar_normalizacion.py
```

En Windows, PowerShell:

```powershell
.\venv\Scripts\python.exe tools\probar_normalizacion.py
```

Si no tienes un entorno virtual, puedes usar el Python compatible del equipo: la herramienta por sí sola no necesita instalar `requirements.txt`.

## Modo texto: descripciones

Es el modo predeterminado. También puede elegirse explícitamente:

```bash
python tools/probar_normalizacion.py --modo texto
```

Introduce una descripción por línea y pulsa Enter. Ejemplos sintéticos:

```text
Entrada: Cámara IP ángulo 2.0 / POE+
Normalizado: CAMARA IP ANGULO 2.0 / POE+
Entrada: SW@24P#RACK
Normalizado: SW 24P RACK
Entrada:   Switch   administrável
Normalizado: SWITCH ADMINISTRAVEL
```

Prueba acentos, mayúsculas/minúsculas, espacios repetidos y signos. Se conservan `+`, `/`, `.` y `-`. Comprueba que `POE` y `POE+`, `CAT6` y `CAT6A`, o `4X1G` y `4X10G` siguen dando resultados diferentes.

`SW 24P` devuelve `SW 24P`, **no** `SWITCH 24P`: la expansión de sinónimos es otra operación de M5a y no se incluye en esta herramienta para evitar dependencia de BD.

## Modo clave: identificadores técnicos

Sal de la sesión anterior y ejecuta:

```bash
python tools/probar_normalizacion.py --modo clave
```

Introduce MPN, SKU de fabricante o códigos de modelo tratados como identificadores exactos:

```text
Entrada: C9200L-24P-4G-E
Normalizado: C9200L24P4GE
Entrada: c9200l 24p 4g e
Normalizado: C9200L24P4GE
Entrada: ABC/123.4
Normalizado: ABC1234
```

**No uses este modo para descripciones.** Elimina todos los signos, incluido el `+` de `POE+`, y conserva únicamente letras ASCII `A-Z` y números. No aplica la descomposición Unicode usada en modo texto.

## Salir y obtener ayuda

- Escribe `salir` y pulsa Enter; esa palabra se reserva como comando.
- También puedes pulsar Ctrl+C, o finalizar la entrada con Ctrl+D en macOS.
- Una línea vacía solicita otra entrada.
- No interpreta JSON ni campos de producto: cada línea es una cadena literal.

```bash
python tools/probar_normalizacion.py --help
```

## Ejecutar las pruebas de la herramienta

Desde la raíz, sin BD ni variables de entorno:

```bash
python -m unittest discover -s tools -p 'test_*.py' -v
```

Estas pruebas revisan entradas repetidas, ambos modos, salida, ayuda y ejecución desde otra carpeta. No sustituyen las pruebas de integración de catálogo con PostgreSQL.

## Cómo interpretar el resultado

La normalización uniforma escritura; **no demuestra que dos productos sean equivalentes**. Las asociaciones, las guardas de variantes técnicas y la comparación de ofertas corresponden a módulos posteriores. Esta utilidad se ejecuta manualmente y no está conectada al servidor, las URLs o los modelos de Django.
