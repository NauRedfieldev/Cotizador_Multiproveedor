"""Prueba manual de las funciones reales de M5a, sin configurar Django."""

import argparse
from pathlib import Path
import sys

# Permite ejecutar el archivo directamente, incluso desde otra carpeta.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.catalog.normalizer import normalizar, normalizar_clave


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prueba interactiva de normalización M5a, sin BD ni red.",
    )
    parser.add_argument(
        "--modo", choices=("texto", "clave"), default="texto",
        help="texto: descripciones (predeterminado); clave: identificadores exactos",
    )
    args = parser.parse_args()
    funcion = normalizar if args.modo == "texto" else normalizar_clave

    print(f"Modo: {args.modo}. Escribe 'salir' para terminar (también Ctrl+C).")
    print("Solo normaliza; no hace matching ni expande sinónimos.")
    if args.modo == "clave":
        print("AVISO: elimina todos los signos, incluido +. No uses descripciones aquí.")

    while True:
        try:
            entrada = input("Entrada: ")
        except (EOFError, KeyboardInterrupt):
            print("\nFin.")
            return 0
        if entrada.strip().casefold() == "salir":
            print("Fin.")
            return 0
        if not entrada.strip():
            print("Entrada vacía: escribe un texto o 'salir'.")
            continue
        print(f"Normalizado: {funcion(entrada)}")


if __name__ == "__main__":
    raise SystemExit(main())
