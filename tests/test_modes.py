
import pytest

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


def _doc(text: str) -> extract.Doc:
    page = extract.Page(number=1, text=text, lines=[], tables=[], has_text=True)
    return extract.Doc(metadata={"page_count": 1}, pages=[page])


def test_schema_captura_campo_por_regex():
    doc = _doc("Ley 12/2023, de 5 de marzo, de cosas.")
    rules = modes.compile_rules(
        [{"name": "numero", "kind": "regex", "pattern": r"Ley\s+(\d+/\d{4})"}]
    )

    out = modes.schema(doc, rules)

    assert out["mode"] == "schema"
    assert out["fields"]["numero"] == "12/2023"
    assert out["unmatched"] == []
    assert out["rules_applied"] == [
        {"name": "numero", "kind": "regex", "pattern": r"Ley\s+(\d+/\d{4})"}
    ]
    assert "rule_errors" not in out["summary"]


def test_schema_campo_sin_coincidencia_es_null_y_unmatched():
    doc = _doc("Un texto cualquiera.")
    rules = modes.compile_rules(
        [{"name": "fecha", "kind": "regex", "pattern": r"Fecha:\s*(\d{4})"}]
    )

    out = modes.schema(doc, rules)

    assert out["fields"] == {"fecha": None}
    assert out["unmatched"] == ["fecha"]


def test_schema_regla_label_captura_resto_de_linea():
    doc = _doc("Base imponible: 1.234,00 EUR\nTotal: 1.493,14 EUR\n")
    rules = modes.compile_rules(
        [{"name": "base", "kind": "label", "pattern": "Base imponible"}]
    )

    out = modes.schema(doc, rules)

    assert out["fields"]["base"] == "1.234,00 EUR"


def test_compile_rules_rechaza_regex_sin_grupo():
    with pytest.raises(modes.RuleError, match="grupo de captura"):
        modes.compile_rules([{"name": "x", "kind": "regex", "pattern": r"Ley\s+\d+"}])


def test_compile_rules_rechaza_patron_invalido():
    with pytest.raises(modes.RuleError, match="inválido"):
        modes.compile_rules([{"name": "x", "kind": "regex", "pattern": "(sin cerrar"}])


def test_compile_rules_rechaza_patron_demasiado_largo():
    with pytest.raises(modes.RuleError, match="caracteres"):
        modes.compile_rules(
            [{"name": "x", "kind": "regex", "pattern": "(" + "a" * 250 + ")"}]
        )


def test_compile_rules_rechaza_demasiadas_reglas():
    muchas = [
        {"name": f"c{i}", "kind": "regex", "pattern": r"(\d)"}
        for i in range(modes.MAX_RULES + 1)
    ]
    with pytest.raises(modes.RuleError, match="reglas"):
        modes.compile_rules(muchas)


def test_schema_patron_catastrofico_expira_sin_colgarse():
    # (a|aa)+ hace backtracking exponencial en la librería regex; (a+)+ no, porque
    # regex lo optimiza. Si este patrón deja de ser lento, buscar otro: lo que se
    # prueba es que el timeout corta y el fallo se reporta, no este patrón concreto.
    doc = _doc("a" * 34 + "b")
    rules = modes.compile_rules(
        [{"name": "bomba", "kind": "regex", "pattern": r"(a|aa)+$"}]
    )

    out = modes.schema(doc, rules)

    assert out["fields"]["bomba"] is None
    assert out["unmatched"] == ["bomba"]
    assert out["summary"]["rule_errors"][0]["name"] == "bomba"


def test_auto_elige_layout_en_un_documento_con_estructura(make_pdf):
    path = make_pdf(
        "ley.pdf",
        [
            [
                ("CAPÍTULO I", 16, True),
                ("Artículo 1. Objeto.", 14, True),
                ("Esta ley regula el asunto.", 11, False),
                ("Artículo 2. Ámbito.", 14, True),
                ("Se aplica en todo el territorio.", 11, False),
            ]
        ],
    )
    doc = extract.parse(path, with_tables=True)

    out = modes.auto(doc)

    assert out["mode"] == "layout"
    assert out["summary"]["auto"]["elegido"] == "layout"
    assert out["summary"]["auto"]["titulos_detectados"] == 3
    assert out["sections"][0]["heading"] == "CAPÍTULO I"


def test_auto_elige_raw_en_texto_corrido(make_pdf):
    path = make_pdf(
        "carta.pdf",
        [
            [
                ("Estimado cliente, le escribimos para informarle.", 11, False),
                ("Quedamos a su disposición para cualquier duda.", 11, False),
            ]
        ],
    )
    doc = extract.parse(path, with_tables=True)

    out = modes.auto(doc)

    assert out["mode"] == "raw"
    assert out["summary"]["auto"]["elegido"] == "raw"
    assert "menos de los 3" in out["summary"]["auto"]["motivo"]
    assert "Estimado cliente" in out["pages"][0]["text"]


def test_auto_no_estructura_por_un_titulo_suelto(make_pdf):
    """Un membrete en negrita no convierte una carta en un documento con secciones."""
    path = make_pdf(
        "membrete.pdf",
        [
            [
                ("INFORME ANUAL", 18, True),
                ("El ejercicio se ha cerrado con normalidad.", 11, False),
                ("No hay incidencias que reseñar.", 11, False),
            ]
        ],
    )
    doc = extract.parse(path, with_tables=True)

    out = modes.auto(doc)

    assert out["mode"] == "raw"
    assert out["summary"]["auto"]["titulos_detectados"] == 1


def test_auto_elige_raw_si_no_hay_texto(make_pdf):
    path = make_pdf("escaneado.pdf", [[], []])
    doc = extract.parse(path, with_tables=True)

    out = modes.auto(doc)

    assert out["mode"] == "raw"
    assert out["summary"]["auto"]["motivo"] == "el documento no tiene texto extraíble"
    assert out["summary"]["pages_without_text"] == [1, 2]
