"""Plantillas de reglas precargadas para el modo schema.

El usuario las ve rellenas en el formulario y puede editarlas o borrarlas antes de
enviar. Todas deben compilar con modes.compile_rules: lo verifica un test.
"""

TEMPLATES: dict[str, dict] = {
    "ley_boe": {
        "label": "Ley / BOE",
        "rules": [
            {
                "name": "numero",
                "kind": "regex",
                "pattern": r"(?:Ley|Real\s+Decreto(?:-ley)?)\s+(\d+/\d{4})",
            },
            {
                "name": "rango",
                "kind": "regex",
                "pattern": r"^(Ley\s+Org[áa]nica|Ley|Real\s+Decreto-ley|Real\s+Decreto)\b",
            },
            {
                "name": "fecha",
                "kind": "regex",
                "pattern": r"de\s+(\d{1,2}\s+de\s+\w+\s+de\s+\d{4})",
            },
            {"name": "boe_id", "kind": "regex", "pattern": r"(BOE-[A-Z]-\d{4}-\d+)"},
            {
                "name": "entrada_en_vigor",
                "kind": "regex",
                "pattern": r"entrar[áa]\s+en\s+vigor\s+(.+)",
            },
        ],
    },
    "factura": {
        "label": "Factura",
        "rules": [
            {"name": "numero_factura", "kind": "label", "pattern": "Factura"},
            {
                "name": "fecha",
                "kind": "regex",
                "pattern": r"[Ff]echa\D{0,10}(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            },
            {
                "name": "nif",
                "kind": "regex",
                "pattern": r"\b([A-Z]\d{8}|\d{8}[A-Za-z])\b",
            },
            {"name": "base_imponible", "kind": "label", "pattern": "Base imponible"},
            {"name": "total", "kind": "label", "pattern": "Total"},
        ],
    },
    "cv": {
        "label": "Curriculum",
        "rules": [
            {
                "name": "email",
                "kind": "regex",
                "pattern": r"([\w.+-]+@[\w-]+\.[\w.]{2,})",
            },
            {"name": "telefono", "kind": "regex", "pattern": r"(\+?\d[\d\s]{7,14}\d)"},
            {
                "name": "linkedin",
                "kind": "regex",
                "pattern": r"(linkedin\.com/in/[\w-]+)",
            },
        ],
    },
}
