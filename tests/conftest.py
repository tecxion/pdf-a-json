"""Fixtures compartidos. Los PDF de prueba se generan con PyMuPDF: sin binarios en el repo."""
from pathlib import Path

import pymupdf
import pytest


def _write(path: Path, pages: list[list[tuple[str, float, bool]]]) -> None:
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        y = 80.0
        for text, size, bold in lines:
            page.insert_text(
                (72, y),
                text,
                fontsize=size,
                fontname="hebo" if bold else "helv",
            )
            y += 40.0  # separación amplia: cada texto queda en su propia línea
    doc.save(str(path))
    doc.close()


@pytest.fixture
def make_pdf(tmp_path: Path):
    """Factoría: make_pdf("x.pdf", [[("texto", 11, False)], []]) -> Path."""

    def factory(name: str, pages: list[list[tuple[str, float, bool]]]) -> Path:
        path = tmp_path / name
        _write(path, pages)
        return path

    return factory
