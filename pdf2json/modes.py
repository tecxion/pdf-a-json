"""Conversión del modelo interno a los tres JSON de salida.

No depende de la capa web ni de PyMuPDF: solo consume dataclasses de extract.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

import regex

from .extract import Doc, Line

SCHEMA_VERSION = 1

HEADING_SIZE_RATIO = 1.15  # un título mide al menos un 15% más que el cuerpo
HEADING_BOLD_MAX_LEN = 120  # negrita y corto también cuenta como título
PARAGRAPH_GAP_RATIO = 0.6  # hueco > 0.6 * alto de línea = párrafo nuevo

_LEGAL_LEVEL_1 = regex.compile(
    r"^(?:TÍTULO|CAPÍTULO|SECCIÓN|LIBRO)\b"
    r"|^Disposici[óo]n\s+(?:adicional|transitoria|derogatoria|final)",
    regex.IGNORECASE,
)
_LEGAL_LEVEL_2 = regex.compile(r"^Art[íi]culo\s+\d+", regex.IGNORECASE)


def _envelope(doc: Doc, mode: str) -> dict:
    """Las claves comunes a los tres modos."""
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "metadata": doc.metadata,
        "summary": {
            "pages_without_text": [p.number for p in doc.pages if not p.has_text]
        },
    }


def raw(doc: Doc) -> dict:
    out = _envelope(doc, "raw")
    out["pages"] = [
        {"number": p.number, "text": p.text, "ocr_required": not p.has_text}
        for p in doc.pages
    ]
    return out


def _legal_level(text: str) -> int | None:
    """Nivel forzado para texto legal español. Tiene prioridad sobre el tamaño de fuente."""
    if _LEGAL_LEVEL_1.match(text):
        return 1
    if _LEGAL_LEVEL_2.match(text):
        return 2
    return None


def _flatten(sections: list[dict]):
    for section in sections:
        yield section
        yield from _flatten(section["children"])


def layout(doc: Doc) -> dict:
    out = _envelope(doc, "layout")
    numbered: list[tuple[int, Line]] = [
        (p.number, line) for p in doc.pages for line in p.lines
    ]

    if not numbered:
        out["sections"] = []
        out["summary"]["sections_found"] = 0
        return out

    median = statistics.median(line.size for _, line in numbered)

    def is_font_heading(line: Line) -> bool:
        return line.size > median * HEADING_SIZE_RATIO or (
            line.bold and len(line.text) < HEADING_BOLD_MAX_LEN
        )

    # El rango de tamaños candidatos da el nivel: el mayor es 1, el siguiente 2...
    candidate_sizes = sorted(
        {round(line.size, 1) for _, line in numbered if is_font_heading(line)},
        reverse=True,
    )
    rank = {size: index + 1 for index, size in enumerate(candidate_sizes)}

    def level_of(line: Line) -> int | None:
        legal = _legal_level(line.text)
        if legal is not None:
            return legal
        if is_font_heading(line):
            return rank.get(round(line.size, 1), len(candidate_sizes) or 1)
        return None

    sections: list[dict] = []
    stack: list[dict] = []
    headings_found = 0

    def new_section(level: int, heading: str | None, page: int) -> dict:
        return {
            "level": level,
            "heading": heading,
            "text": "",
            "pages": [page],
            "tables": [],
            "children": [],
        }

    def attach(section: dict) -> None:
        while stack and stack[-1]["level"] >= section["level"]:
            stack.pop()
        (stack[-1]["children"] if stack else sections).append(section)
        stack.append(section)

    previous: Line | None = None
    for page_number, line in numbered:
        level = level_of(line)
        if level is not None:
            headings_found += 1
            attach(new_section(level, line.text, page_number))
            previous = line
            continue

        if not stack:  # texto antes de cualquier título
            attach(new_section(1, None, page_number))

        current = stack[-1]
        separator = ""
        if current["text"]:
            separator = " "
            if previous is not None:
                height = previous.bbox[3] - previous.bbox[1]
                gap = line.bbox[1] - previous.bbox[3]
                # ponytail: umbral fijo. Si los PDF reales parten mal los párrafos,
                # tocar PARAGRAPH_GAP_RATIO antes de añadir heurísticas nuevas.
                if height > 0 and gap > height * PARAGRAPH_GAP_RATIO:
                    separator = "\n\n"
        current["text"] += separator + line.text
        if page_number not in current["pages"]:
            current["pages"].append(page_number)
        previous = line

    # Cada tabla va a la última sección (en orden de lectura) que toque su página.
    flat = list(_flatten(sections))
    for page in doc.pages:
        for table in page.tables:
            matching = [s for s in flat if table.page in s["pages"]]
            target = matching[-1] if matching else (flat[-1] if flat else None)
            if target is not None:
                target["tables"].append({"page": table.page, "rows": table.rows})

    out["sections"] = sections
    out["summary"]["sections_found"] = headings_found
    return out


MAX_PATTERN_LEN = 200
MAX_RULES = 30
RULE_TIMEOUT_S = 1.0


class RuleError(ValueError):
    """Regla inválida. El mensaje se muestra al usuario; le corresponde un 422."""


@dataclass
class CompiledRule:
    name: str
    kind: str
    pattern: str
    compiled: regex.Pattern


def compile_rules(rules: list[dict]) -> list[CompiledRule]:
    """Valida y compila las reglas ANTES de abrir ningún PDF.

    Los patrones vienen de usuarios anónimos de internet: entrada no confiable.
    """
    if len(rules) > MAX_RULES:
        raise RuleError(
            f"Máximo {MAX_RULES} reglas por petición, has enviado {len(rules)}."
        )

    compiled_rules: list[CompiledRule] = []
    for rule in rules:
        name = (rule.get("name") or "").strip()
        kind = rule.get("kind")
        pattern = rule.get("pattern") or ""

        if not name:
            raise RuleError("Hay una regla sin nombre de campo.")
        if len(pattern) > MAX_PATTERN_LEN:
            raise RuleError(f"{name}: el patrón supera {MAX_PATTERN_LEN} caracteres.")
        if kind == "label":
            source = regex.escape(pattern) + r"[:\s]*(.+)"
        elif kind == "regex":
            source = pattern
        else:
            raise RuleError(f"{name}: tipo de regla desconocido '{kind}'.")

        try:
            compiled = regex.compile(source, regex.MULTILINE)
        except regex.error as exc:
            raise RuleError(f"{name}: patrón inválido ({exc}).") from exc
        if compiled.groups < 1:
            raise RuleError(
                f"{name}: el patrón necesita un grupo de captura, por ejemplo (\\d+)."
            )

        compiled_rules.append(
            CompiledRule(name=name, kind=kind, pattern=pattern, compiled=compiled)
        )
    return compiled_rules


def schema(doc: Doc, rules: list[CompiledRule]) -> dict:
    out = _envelope(doc, "schema")
    text = "\n".join(page.text for page in doc.pages)

    fields: dict[str, str | None] = {}
    unmatched: list[str] = []
    errors: list[dict] = []

    for rule in rules:
        try:
            match = rule.compiled.search(text, timeout=RULE_TIMEOUT_S)
        except TimeoutError:
            fields[rule.name] = None
            unmatched.append(rule.name)
            errors.append(
                {
                    "name": rule.name,
                    "error": f"la regla superó {RULE_TIMEOUT_S}s y se abortó",
                }
            )
            continue

        value = match.group(1) if match else None
        value = value.strip() if value else None
        fields[rule.name] = value
        if value is None:
            unmatched.append(rule.name)

    out["fields"] = fields
    out["unmatched"] = unmatched
    out["rules_applied"] = [
        {"name": r.name, "kind": r.kind, "pattern": r.pattern} for r in rules
    ]
    if errors:
        out["summary"]["rule_errors"] = errors
    return out
