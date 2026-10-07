from pdf2json import extract, modes
from pdf2json.templates_rules import TEMPLATES


def test_las_plantillas_esperadas_existen():
    assert set(TEMPLATES) == {"ley_boe", "factura", "cv"}
    for key, template in TEMPLATES.items():
        assert template["label"], key
        assert template["rules"], key


def test_todas_las_reglas_de_plantilla_compilan():
    for template in TEMPLATES.values():
        modes.compile_rules(template["rules"])  # no debe lanzar RuleError


def test_plantilla_ley_boe_extrae_de_un_encabezado_real():
    texto = (
        "Ley 12/2023, de 24 de mayo, por el derecho a la vivienda.\n"
        "BOE-A-2023-12203\n"
        "Esta ley entrará en vigor el día siguiente al de su publicación.\n"
    )
    page = extract.Page(number=1, text=texto, lines=[], tables=[], has_text=True)
    doc = extract.Doc(metadata={"page_count": 1}, pages=[page])

    out = modes.schema(doc, modes.compile_rules(TEMPLATES["ley_boe"]["rules"]))

    assert out["fields"]["numero"] == "12/2023"
    assert out["fields"]["boe_id"] == "BOE-A-2023-12203"
