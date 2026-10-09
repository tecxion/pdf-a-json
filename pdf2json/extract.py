"""Lectura de PDF a un modelo de documento interno.

No depende de la capa web ni la conoce. Único punto del proyecto que importa PyMuPDF.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import pymupdf

from .errors import (  # noqa: F401
    CorruptPdf,
    EncryptedPdf,
    PdfError,
    SinTexto,
    TooManyPages,
)

log = logging.getLogger("pdf2json.extract")

BOLD_FLAG = 1 << 4  # bit 4 de span["flags"] en PyMuPDF

OCR_DPI = 200  # suficiente para texto impreso; más alto dispara el tiempo
_ocr_no_disponible = False  # se avisa una vez, no en cada página


@dataclass
class Line:
    text: str
    size: float
    bold: bool
    bbox: tuple[float, float, float, float]


@dataclass
class Table:
    page: int
    rows: list[list[str]]


@dataclass
class Page:
    number: int  # 1-indexado
    text: str = ""
    lines: list[Line] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    has_text: bool = False
    ocr: bool = False  # el texto salió de reconocimiento óptico, no del PDF


@dataclass
class Doc:
    metadata: dict
    pages: list[Page]


def _metadata(doc: pymupdf.Document) -> dict:
    meta = doc.metadata or {}
    return {
        "title": meta.get("title") or None,
        "author": meta.get("author") or None,
        "subject": meta.get("subject") or None,
        "creator": meta.get("creator") or None,
        "producer": meta.get("producer") or None,
        "creation_date": meta.get("creationDate") or None,
        "mod_date": meta.get("modDate") or None,
        "page_count": doc.page_count,
        "encrypted": bool(meta.get("encryption")),
    }


def _lines(page: pymupdf.Page) -> list[Line]:
    """Una Line por línea de texto.

    Se trabaja por línea y no por bloque de PyMuPDF a propósito: el bloque une un
    título con el párrafo que le sigue cuando están cerca, y eso rompe la detección
    de títulos del modo layout.
    """
    out: list[Line] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:  # 0 = texto, 1 = imagen
            continue
        for line in block["lines"]:
            spans = [span for span in line["spans"] if span["text"].strip()]
            if not spans:
                continue
            text = "".join(span["text"] for span in spans).strip()
            if not text:
                continue
            out.append(
                Line(
                    text=text,
                    size=max(span["size"] for span in spans),
                    bold=any(span["flags"] & BOLD_FLAG for span in spans),
                    bbox=tuple(line["bbox"]),
                )
            )
    return out


def _tables(page: pymupdf.Page, number: int) -> list[Table]:
    found: list[Table] = []
    for table in page.find_tables().tables:
        rows = [
            ["" if cell is None else str(cell) for cell in row]
            for row in table.extract()
        ]
        if rows:
            found.append(Table(page=number, rows=rows))
    return found


def _open(path: str | os.PathLike) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path)
    except pymupdf.FileDataError as exc:
        raise CorruptPdf(f"PDF corrupto o ilegible: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise EncryptedPdf("PDF protegido con contraseña: no se puede leer")
    return doc


def page_count(path: str | os.PathLike) -> int:
    """Número de páginas sin parsear el contenido. Para validar límites barato."""
    with _open(path) as doc:
        return doc.page_count


def _texto_ocr(page: pymupdf.Page, idioma: str) -> str:
    """Texto de una página escaneada. Devuelve "" si Tesseract no está instalado.

    Se avisa una sola vez: con OCR activado y Tesseract ausente, cada página
    fallaría igual y el registro sería ilegible.
    """
    global _ocr_no_disponible
    if _ocr_no_disponible:
        return ""
    try:
        return page.get_text("text", textpage=page.get_textpage_ocr(
            language=idioma, dpi=OCR_DPI, full=False
        ))
    except RuntimeError as exc:
        _ocr_no_disponible = True
        log.warning("OCR activado pero no utilizable: %s", exc)
        return ""


def parse(
    path: str | os.PathLike,
    with_tables: bool = False,
    ocr: bool = False,
    ocr_idioma: str = "spa",
) -> Doc:
    """Lee el PDF completo.

    `with_tables` solo lo necesita el modo layout: find_tables es caro.
    `ocr` solo se aplica a las páginas que no tienen texto, porque es lentísimo
    comparado con leer la capa de texto.
    """
    with _open(path) as doc:
        pages: list[Page] = []
        for number, page in enumerate(doc, start=1):
            text = page.get_text("text")
            reconocido = False
            if ocr and not text.strip():
                text = _texto_ocr(page, ocr_idioma)
                reconocido = bool(text.strip())
            pages.append(
                Page(
                    number=number,
                    text=text,
                    lines=_lines(page),
                    tables=_tables(page, number) if with_tables else [],
                    has_text=bool(text.strip()),
                    ocr=reconocido,
                )
            )
        return Doc(metadata=_metadata(doc), pages=pages)
