
from pdf2json import extract, modes


def test_raw_devuelve_texto_por_pagina(make_pdf):
    path = make_pdf(
        "raw.pdf",
        [[("Primera página", 11, False)], [], [("Tercera página", 11, False)]],
    )
    doc = extract.parse(path)

    out = modes.raw(doc)

    assert out["schema_version"] == 1
    assert out["mode"] == "raw"
    assert out["metadata"]["page_count"] == 3
    assert [p["number"] for p in out["pages"]] == [1, 2, 3]
    assert "Primera página" in out["pages"][0]["text"]
    assert out["pages"][1]["text"].strip() == ""
    assert out["pages"][1]["ocr_required"] is True
    assert out["pages"][0]["ocr_required"] is False
    assert out["summary"]["pages_without_text"] == [2]


def test_layout_anida_articulo_bajo_capitulo(make_pdf):
    path = make_pdf(
        "ley.pdf",
        [
            [
                ("CAPÍTULO I", 16, True),
                ("Artículo 1. Objeto.", 14, True),
                ("Esta ley tiene por objeto regular el asunto.", 11, False),
            ]
        ],
    )
    doc = extract.parse(path, with_tables=True)

    out = modes.layout(doc)

    assert out["mode"] == "layout"
    assert len(out["sections"]) == 1
    capitulo = out["sections"][0]
    assert capitulo["level"] == 1
    assert capitulo["heading"] == "CAPÍTULO I"
    assert len(capitulo["children"]) == 1
    articulo = capitulo["children"][0]
    assert articulo["level"] == 2
    assert articulo["heading"] == "Artículo 1. Objeto."
    assert "por objeto regular" in articulo["text"]
    assert articulo["pages"] == [1]
    assert out["summary"]["sections_found"] == 2


def test_layout_sin_titulos_degrada_a_una_seccion(make_pdf):
    path = make_pdf("plano.pdf", [[("Texto suelto sin ningun titulo.", 11, False)]])
    doc = extract.parse(path, with_tables=True)

    out = modes.layout(doc)

    assert len(out["sections"]) == 1
    assert out["sections"][0]["heading"] is None
    assert out["sections"][0]["level"] == 1
    assert "Texto suelto" in out["sections"][0]["text"]
    assert out["summary"]["sections_found"] == 0


def test_layout_detecta_disposicion_como_nivel_uno(make_pdf):
    path = make_pdf(
        "disp.pdf",
        [
            [
                ("Artículo 1. Objeto.", 14, True),
                ("Disposición final primera. Entrada en vigor.", 14, True),
                ("Entrará en vigor al día siguiente.", 11, False),
            ]
        ],
    )
    doc = extract.parse(path, with_tables=True)

    out = modes.layout(doc)

    niveles = [(s["level"], s["heading"]) for s in out["sections"]]
    assert niveles[0] == (2, "Artículo 1. Objeto.")
    assert niveles[1][0] == 1
    assert niveles[1][1].startswith("Disposición final primera")
    assert "al día siguiente" in out["sections"][1]["text"]


def test_layout_sin_paginas_no_revienta():
    doc = extract.Doc(metadata={"page_count": 0}, pages=[])

    out = modes.layout(doc)

    assert out["sections"] == []
    assert out["summary"]["sections_found"] == 0
