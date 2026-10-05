"""Bloque G: reglas estáticas del repositorio.

Se analiza el código con ast/tokenize (no con búsquedas de texto) y se excluyen las pruebas,
__pycache__ y este archivo.
"""
import ast
import re
import tokenize
from io import StringIO
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import SimpleTestCase, tag

RAIZ = Path(settings.BASE_DIR)
ESTE_ARCHIVO = Path(__file__).resolve()
NOMBRE_VALIDO = re.compile(r"^[a-z0-9_]+$")
EXTENSIONES_CON_NOMBRE_REGULADO = {".py", ".html", ".css", ".js"}
PATRON_CREDENCIAL = re.compile(r"api_key|secret|password|client_secret|username")
TOKENS_DE_TEXTO = {
    tokenize.NAME,
    tokenize.STRING,
    getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING),
    getattr(tokenize, "TSTRING_MIDDLE", tokenize.STRING),
}


def es_prueba(ruta):
    partes = ruta.relative_to(RAIZ).parts
    return "tests" in partes or ruta.name == "tests.py" or ruta.name.startswith("test_")


def archivos_python(*carpetas):
    """Archivos .py de producción: sin pruebas, sin __pycache__ y sin este archivo."""
    for carpeta in carpetas:
        for ruta in sorted((RAIZ / carpeta).rglob("*.py")):
            if "__pycache__" in ruta.parts or es_prueba(ruta) or ruta.resolve() == ESTE_ARCHIVO:
                continue
            yield ruta


def arbol(ruta):
    return ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))


def ubicacion(ruta, linea):
    return f"{ruta.relative_to(RAIZ).as_posix()}:{linea}"


def nombres_usados(modulo):
    """Cada nombre que aparece como variable, atributo o import."""
    for nodo in ast.walk(modulo):
        if isinstance(nodo, ast.Name):
            yield nodo, nodo.id
        elif isinstance(nodo, ast.Attribute):
            yield nodo, nodo.attr
        elif isinstance(nodo, (ast.Import, ast.ImportFrom)):
            for alias in nodo.names:
                yield nodo, alias.name.split(".")[-1]


def modulos_importados(ruta, nodo):
    """Ruta absoluta de los módulos que importa un nodo, resolviendo los imports relativos."""
    if isinstance(nodo, ast.Import):
        return [alias.name for alias in nodo.names]
    if isinstance(nodo, ast.ImportFrom):
        paquete = list(ruta.relative_to(RAIZ).with_suffix("").parts[:-1])
        base = paquete[: len(paquete) - nodo.level + 1] if nodo.level else []
        return [".".join(base + ([nodo.module] if nodo.module else []))]
    return []


def tokens_de_codigo(ruta):
    """Nombres y textos del código; los comentarios no cuentan como código activo."""
    with ruta.open("rb") as archivo:
        for token in tokenize.tokenize(archivo.readline):
            if token.type in TOKENS_DE_TEXTO:
                yield token


@tag("unit")
class PruebasReglasDelRepositorio(SimpleTestCase):
    def test_apps_no_usa_taskgroup_ni_threadpoolexecutor(self):
        """G1 ⭐ Ningún módulo de apps/ usa TaskGroup ni ThreadPoolExecutor."""
        # Act
        usos = [
            ubicacion(ruta, nodo.lineno)
            for ruta in archivos_python("apps")
            for nodo, nombre in nombres_usados(arbol(ruta))
            if nombre in {"TaskGroup", "ThreadPoolExecutor"}
        ]

        # Assert
        self.assertEqual(usos, [])

    def test_providers_no_llama_a_float(self):
        """G2 ⭐ apps/providers no llama a float(): el dinero va en Decimal."""
        # Act
        llamadas = [
            ubicacion(ruta, nodo.lineno)
            for ruta in archivos_python("apps/providers")
            for nodo in ast.walk(arbol(ruta))
            if isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Name)
            and nodo.func.id == "float"
        ]

        # Assert
        self.assertEqual(llamadas, [])

    def test_no_hay_integracion_cisco_en_codigo_activo(self):
        """G3 Ni apps/ ni settings/ mencionan Cisco en código activo (los comentarios no cuentan)."""
        # Act
        referencias = [
            ubicacion(ruta, token.start[0])
            for ruta in archivos_python("apps", "settings")
            for token in tokens_de_codigo(ruta)
            if "cisco" in token.string.lower()
        ]

        # Assert
        self.assertEqual(referencias, [])

    def test_providers_no_importa_de_catalog_ni_de_quotes(self):
        """G4 apps/providers no importa de apps.catalog ni de apps.quotes (dirección quotes → catalog → providers)."""
        # Act
        importaciones = [
            ubicacion(ruta, nodo.lineno)
            for ruta in archivos_python("apps/providers")
            for nodo in ast.walk(arbol(ruta))
            for modulo in modulos_importados(ruta, nodo)
            if modulo.startswith(("apps.catalog", "apps.quotes"))
        ]

        # Assert
        self.assertEqual(importaciones, [])

    def test_archivos_y_carpetas_cumplen_snake_case(self):
        """G5 Archivos .py/.html/.css/.js y carpetas de apps/ y settings/ cumplen ^[a-z0-9_]+$ (migraciones incluidas)."""
        # Act
        incumplen = [
            ruta.relative_to(RAIZ).as_posix()
            for carpeta in ("apps", "settings")
            for ruta in sorted((RAIZ / carpeta).rglob("*"))
            if (ruta.is_dir() and not NOMBRE_VALIDO.match(ruta.name))
            or (ruta.suffix in EXTENSIONES_CON_NOMBRE_REGULADO and not NOMBRE_VALIDO.match(ruta.stem))
        ]

        # Assert
        self.assertEqual(incumplen, [])

    def test_no_se_usa_jinja2(self):
        """G6 Jinja2 no está en los requirements ni como backend de plantillas."""
        # Act
        paquetes = [
            linea.strip()
            for archivo in sorted(RAIZ.glob("requirements*.txt"))
            for linea in archivo.read_text(encoding="utf-8-sig").splitlines()
            if linea.strip().lower().startswith("jinja2")
        ]
        backends = [
            plantilla["BACKEND"]
            for plantilla in settings.TEMPLATES
            if "jinja2" in plantilla["BACKEND"].lower()
        ]

        # Assert
        self.assertEqual((paquetes, backends), ([], []))

    def test_manage_py_check_no_reporta_errores(self):
        """G7 manage.py check termina sin errores."""
        # Arrange
        salida = StringIO()
        error = None

        # Act
        try:
            call_command("check", stdout=salida, stderr=salida)
        except SystemCheckError as exc:
            error = str(exc)

        # Assert
        self.assertIsNone(error)

    def test_modelos_de_providers_no_tienen_campos_de_credencial(self):
        """G8 Ningún modelo de providers tiene campos con nombre de credencial; access_token es el único secreto."""
        # Act
        campos = [
            f"{modelo.__name__}.{campo.name}"
            for modelo in apps.get_app_config("providers").get_models()
            for campo in modelo._meta.get_fields()
            if PATRON_CREDENCIAL.search(campo.name.lower())
        ]

        # Assert
        self.assertEqual(campos, [])

    def test_providers_sin_tests_py_suelto_ni_referencias_a_polls(self):
        """G9 Sin restos del tutorial: no hay apps/providers/tests.py ni referencias a polls."""
        # Act
        tests_py_suelto = (RAIZ / "apps" / "providers" / "tests.py").exists()
        referencias_polls = [
            ubicacion(ruta, token.start[0])
            for ruta in archivos_python("apps", "settings")
            for token in tokens_de_codigo(ruta)
            if "polls" in token.string.lower()
        ]

        # Assert
        self.assertEqual((tests_py_suelto, referencias_polls), (False, []))
