"""Extracción de datos de páginas HTML con la biblioteca estándar (SDD M9, docs/31-sdd-m9-exel-web.md).

Es genérica: no sabe de ningún proveedor. Sin dependencias nuevas (html.parser, nunca BeautifulSoup
ni lxml).
"""
from __future__ import annotations

from html.parser import HTMLParser

# Lo que un navegador envía de un <input> sin pulsar nada; los botones solo cuentan si se pulsan.
_TIPOS_QUE_SE_ENVIAN = {"hidden", "text", "password", "email", "search", "tel", "number"}


class _Formulario(HTMLParser):
    def __init__(self, form_id: str) -> None:
        super().__init__()  # convert_charrefs: el texto y los atributos llegan ya sin entidades
        self.form_id = form_id
        self.campos: dict[str, str] | None = None
        self._dentro = False
        self._select: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        atributos = dict(attrs)
        if tag == "form":
            self._dentro = atributos.get("id") == self.form_id and self.campos is None
            if self._dentro:
                self.campos = {}
            return
        if not self._dentro:
            return
        nombre = atributos.get("name")
        if tag == "input" and nombre and (atributos.get("type") or "text").lower() in _TIPOS_QUE_SE_ENVIAN:
            self.campos[nombre] = atributos.get("value") or ""
        elif tag == "select":
            self._select = nombre
        elif tag == "option" and self._select:
            valor = atributos.get("value") or ""
            # La primera opción vale si ninguna lleva selected; una posterior con selected la sustituye.
            if self._select not in self.campos or "selected" in atributos:
                self.campos[self._select] = valor

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._select = None
        elif tag == "form":
            self._dentro = False
            self._select = None


def campos_de_formulario(html: str, form_id: str) -> dict[str, str] | None:
    """Los campos que un navegador enviaría con el formulario `form_id` sin pulsar ningún botón:
    los inputs con name (hidden, text, password…) y la opción elegida de cada select.

    None si la página no tiene ese formulario.
    """
    analizador = _Formulario(form_id)
    analizador.feed(html)
    analizador.close()
    return analizador.campos
