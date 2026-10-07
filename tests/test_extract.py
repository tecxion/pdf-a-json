import pymupdf
import pytest

from pdf2json import extract


def test_parse_numera_paginas_desde_uno(make_pdf):
    path = make_pdf(
        "tres.pdf",
        [
            [("Página uno", 11, False)],
            [("Página dos", 11, False)],
            [("Página tres", 11, False)],
        ],
    )

    doc = extract.parse(path)

    assert [p.number for p in doc.pages] == [1, 2, 3]
    assert doc.metadata["page_count"] == 3
    assert "Página dos" in doc.pages[1].text


def test_pagina_vacia_marca_has_text_false(make_pdf):
    path = make_pdf("mixto.pdf", [[("Con texto", 11, False)], []])

    doc = extract.parse(path)

    assert doc.pages[0].has_text is True
    assert doc.pages[1].has_text is False


def test_lineas_llevan_tamano_y_negrita(make_pdf):
    path = make_pdf("estilos.pdf", [[("TITULO", 16, True), ("cuerpo", 11, False)]])

    lines = extract.parse(path).pages[0].lines

    assert [line.text for line in lines] == ["TITULO", "cuerpo"]
    assert lines[0].bold is True
    assert lines[0].size > lines[1].size
    assert lines[1].bold is False


def test_pdf_cifrado_lanza_encrypted(make_pdf, tmp_path):
    origen = make_pdf("claro.pdf", [[("secreto", 11, False)]])
    cifrado = tmp_path / "cifrado.pdf"
    doc = pymupdf.open(origen)
    doc.save(
        str(cifrado),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="propietario",
        user_pw="usuario",
    )
    doc.close()

    with pytest.raises(extract.EncryptedPdf):
        extract.parse(cifrado)


def test_pdf_corrupto_lanza_corrupt(tmp_path):
    roto = tmp_path / "roto.pdf"
    roto.write_bytes(b"%PDF-1.7\nesto no es un PDF\n")

    with pytest.raises(extract.CorruptPdf):
        extract.parse(roto)


def test_page_count_no_parsea_el_documento(make_pdf):
    path = make_pdf("dos.pdf", [[("a", 11, False)], [("b", 11, False)]])

    assert extract.page_count(path) == 2
