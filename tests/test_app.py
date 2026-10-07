import glob
import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pdf2json import app as app_module


@pytest.fixture
def client():
    with TestClient(app_module.app) as test_client:
        yield test_client


def tmp_dirs() -> list[str]:
    """Directorios temporales que la app podría haber dejado atrás."""
    return glob.glob(os.path.join(tempfile.gettempdir(), app_module.TMP_PREFIX + "*"))


def upload(path: Path):
    return ("files", (path.name, path.read_bytes(), "application/pdf"))


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_index_responde_html(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_convert_un_pdf_devuelve_json_descargable(client, make_pdf):
    path = make_pdf("uno.pdf", [[("Hola mundo", 11, False)]])

    response = client.post("/convert", data={"mode": "raw"}, files=[upload(path)])

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    assert "uno.json" in response.headers["content-disposition"]
    body = json.loads(response.content)
    assert body["mode"] == "raw"
    assert "Hola mundo" in body["pages"][0]["text"]
    assert not tmp_dirs()


def test_convert_modo_layout(client, make_pdf):
    path = make_pdf(
        "ley.pdf",
        [[("Artículo 1. Objeto.", 14, True), ("El objeto es este.", 11, False)]],
    )

    response = client.post("/convert", data={"mode": "layout"}, files=[upload(path)])

    body = json.loads(response.content)
    assert body["mode"] == "layout"
    assert body["sections"][0]["heading"] == "Artículo 1. Objeto."
    assert not tmp_dirs()


def test_convert_modo_schema_con_regla(client, make_pdf):
    path = make_pdf("ley.pdf", [[("Ley 12/2023, de cosas.", 11, False)]])

    response = client.post(
        "/convert",
        data={
            "mode": "schema",
            "rule_name": "numero",
            "rule_kind": "regex",
            "rule_pattern": r"Ley\s+(\d+/\d{4})",
        },
        files=[upload(path)],
    )

    body = json.loads(response.content)
    assert body["fields"]["numero"] == "12/2023"
    assert not tmp_dirs()


def test_convert_rechaza_fichero_que_no_es_pdf(client, tmp_path):
    falso = tmp_path / "falso.pdf"
    falso.write_bytes(b"no soy un pdf")

    response = client.post("/convert", data={"mode": "raw"}, files=[upload(falso)])

    assert response.status_code == 422
    assert not tmp_dirs()


def test_convert_rechaza_fichero_grande(client, make_pdf, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_FILE_BYTES", 100)
    path = make_pdf("grande.pdf", [[("x" * 500, 11, False)]])

    response = client.post("/convert", data={"mode": "raw"}, files=[upload(path)])

    assert response.status_code == 413
    assert not tmp_dirs()


def test_convert_rechaza_demasiadas_paginas(client, make_pdf, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_PAGES", 1)
    path = make_pdf("dos.pdf", [[("a", 11, False)], [("b", 11, False)]])

    response = client.post("/convert", data={"mode": "raw"}, files=[upload(path)])

    assert response.status_code == 413
    assert not tmp_dirs()


def test_convert_rechaza_pdf_cifrado(client, make_pdf, tmp_path):
    import pymupdf

    claro = make_pdf("claro.pdf", [[("secreto", 11, False)]])
    cifrado = tmp_path / "cifrado.pdf"
    doc = pymupdf.open(claro)
    doc.save(
        str(cifrado),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="p",
        user_pw="u",
    )
    doc.close()

    response = client.post("/convert", data={"mode": "raw"}, files=[upload(cifrado)])

    assert response.status_code == 422
    assert "contraseña" in response.text
    assert not tmp_dirs()


def test_convert_regex_invalido_devuelve_422_antes_de_leer(client, make_pdf):
    path = make_pdf("uno.pdf", [[("texto", 11, False)]])

    response = client.post(
        "/convert",
        data={
            "mode": "schema",
            "rule_name": "x",
            "rule_kind": "regex",
            "rule_pattern": "(sin cerrar",
        },
        files=[upload(path)],
    )

    assert response.status_code == 422
    assert "x" in response.text
    assert not tmp_dirs()


def test_convert_sin_ficheros_devuelve_422(client):
    response = client.post("/convert", data={"mode": "raw"})

    assert response.status_code == 422
    assert not tmp_dirs()


def test_convert_modo_desconocido_devuelve_422(client, make_pdf):
    path = make_pdf("uno.pdf", [[("texto", 11, False)]])

    response = client.post("/convert", data={"mode": "inventado"}, files=[upload(path)])

    assert response.status_code == 422
    assert not tmp_dirs()


def test_safe_name_sanea_el_nombre():
    assert app_module._safe_name("../../etc/passwd", ".json") == "passwd.json"
    assert app_module._safe_name('ley "12-2023".pdf', ".json") == "ley__12-2023.json"
    assert app_module._safe_name(None, ".json") == "documento.json"
    assert app_module._safe_name("   ", ".json") == "documento.json"


def test_safe_name_no_deja_escapar_separadores():
    """Lo que importa: el nombre va a un Content-Disposition, no puede llevar rutas."""
    for malicioso in ('ley "12/2023".pdf', "..\\..\\x.pdf", "a/b/c.pdf", "/etc/passwd"):
        salida = app_module._safe_name(malicioso, ".json")

        assert "/" not in salida
        assert "\\" not in salida
        assert salida.endswith(".json")
