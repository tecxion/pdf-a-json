import glob
import io
import json
import os
import zipfile
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pdf2json import app as app_module


@pytest.fixture(autouse=True)
def limitador_limpio():
    """El rate limit cuenta por IP y TestClient siempre usa la misma.

    Sin este reinicio, los últimos tests del fichero reciben 429 por culpa de las
    peticiones de los anteriores. El límite en sí se prueba aparte.
    """
    app_module.limiter.reset()
    yield


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


def test_convert_dos_pdf_devuelve_zip(client, make_pdf):
    uno = make_pdf("uno.pdf", [[("Uno", 11, False)]])
    dos = make_pdf("dos.pdf", [[("Dos", 11, False)]])

    response = client.post(
        "/convert", data={"mode": "raw"}, files=[upload(uno), upload(dos)]
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == ["dos.json", "uno.json"]
        body = json.loads(archive.read("uno.json"))
        assert "Uno" in body["pages"][0]["text"]
    assert not tmp_dirs()


def test_convert_lote_con_un_pdf_roto_sigue_adelante(client, make_pdf, tmp_path):
    bueno = make_pdf("bueno.pdf", [[("Bien", 11, False)]])
    roto = tmp_path / "roto.pdf"
    roto.write_bytes(b"%PDF-1.7\nbasura\n")

    response = client.post(
        "/convert", data={"mode": "raw"}, files=[upload(bueno), upload(roto)]
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "bueno.json" in archive.namelist()
        assert "_errors.json" in archive.namelist()
        errors = json.loads(archive.read("_errors.json"))
        assert errors[0]["filename"] == "roto.pdf"
        assert errors[0]["error"]
    assert not tmp_dirs()


def test_convert_lote_con_nombres_repetidos_no_pisa_entradas(client, make_pdf):
    uno = make_pdf("mismo.pdf", [[("A", 11, False)]])

    response = client.post(
        "/convert", data={"mode": "raw"}, files=[upload(uno), upload(uno)]
    )

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == ["mismo.json", "mismo_2.json"]
    assert not tmp_dirs()


def test_convert_demasiados_ficheros_devuelve_413(client, make_pdf, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_FILES", 1)
    uno = make_pdf("uno.pdf", [[("A", 11, False)]])
    dos = make_pdf("dos.pdf", [[("B", 11, False)]])

    response = client.post(
        "/convert", data={"mode": "raw"}, files=[upload(uno), upload(dos)]
    )

    assert response.status_code == 413
    assert not tmp_dirs()


def test_timeout_corta_la_conversion_y_limpia(client, make_pdf, monkeypatch):
    path = make_pdf("uno.pdf", [[("A", 11, False)]])

    def nunca_termina(*_args, **_kwargs):
        raise app_module.ConvertTimeout("La conversión superó 1s y se canceló.")

    monkeypatch.setattr(app_module, "_run_conversion", nunca_termina)

    response = client.post("/convert", data={"mode": "raw"}, files=[upload(path)])

    assert response.status_code == 504
    assert "canceló" in response.text
    assert not tmp_dirs()


def test_tras_resetear_el_pool_sigue_sirviendo(client, make_pdf):
    """El pool se recrea solo tras shutdown(cancel_futures=True)."""
    path = make_pdf("uno.pdf", [[("A", 11, False)]])
    app_module._reset_pool()

    response = client.post("/convert", data={"mode": "raw"}, files=[upload(path)])

    assert response.status_code == 200
    assert not tmp_dirs()


def test_arranque_borra_temporales_huerfanos():
    huerfano = tempfile.mkdtemp(prefix=app_module.TMP_PREFIX)
    (Path(huerfano) / "basura.pdf").write_bytes(b"%PDF-1.7")

    with TestClient(app_module.app):
        pass

    assert not os.path.exists(huerfano)


def test_rate_limit_devuelve_429(make_pdf, monkeypatch):
    import importlib

    monkeypatch.setenv("RATE_LIMIT", "1/minute")
    from pdf2json import app as fresh

    fresh = importlib.reload(fresh)
    path = make_pdf("uno.pdf", [[("A", 11, False)]])

    try:
        with TestClient(fresh.app) as fresh_client:
            primera = fresh_client.post(
                "/convert", data={"mode": "raw"}, files=[upload(path)]
            )
            segunda = fresh_client.post(
                "/convert", data={"mode": "raw"}, files=[upload(path)]
            )

        assert primera.status_code == 200
        assert segunda.status_code == 429
        assert segunda.headers["retry-after"] == "60"
    finally:
        monkeypatch.undo()
        importlib.reload(fresh)  # deja el módulo con los valores por defecto

    assert not tmp_dirs()


def test_timeout_real_mata_el_proceso_y_el_pool_se_recupera(client, make_pdf, monkeypatch):
    """Recorrido completo: el future no cumple a tiempo, el pool se descarta y se recrea.

    Con un timeout de 1 ms ni siquiera un PDF trivial llega, así que se ejercita el
    camino real (ProcessPoolExecutor + shutdown(cancel_futures=True)) sin depender de
    un PDF patológico.
    """
    path = make_pdf("uno.pdf", [[("A", 11, False)]])
    monkeypatch.setattr(app_module, "CONVERT_TIMEOUT_S", 0.001)

    cortada = client.post("/convert", data={"mode": "raw"}, files=[upload(path)])

    assert cortada.status_code == 504
    assert not tmp_dirs()

    monkeypatch.undo()
    app_module.limiter.reset()

    recuperada = client.post("/convert", data={"mode": "raw"}, files=[upload(path)])

    assert recuperada.status_code == 200
    assert not tmp_dirs()


def test_reset_pool_mata_a_los_workers():
    """Un worker colgado no puede sobrevivir al reinicio del pool."""
    pool = app_module._get_pool()
    pool.submit(len, "arranca el worker").result(timeout=30)
    workers = list(pool._processes.values())
    assert workers
    assert all(worker.is_alive() for worker in workers)

    app_module._reset_pool()

    # No basta con join(): el hilo de gestión del executor también los está
    # recogiendo, y quién gana la carrera varía entre ejecuciones. Lo que importa
    # es que acaben muertos, no cuándo exactamente.
    limite = time.monotonic() + 10
    for worker in workers:
        while worker.is_alive() and time.monotonic() < limite:
            time.sleep(0.05)
        assert not worker.is_alive()


def test_fields_row_devuelve_una_fila_vacia(client):
    response = client.get("/fields/row")

    assert response.status_code == 200
    assert 'name="rule_name"' in response.text
    assert 'name="rule_pattern"' in response.text


def test_fields_template_precarga_las_reglas(client):
    response = client.get("/fields/template/ley_boe")

    assert response.status_code == 200
    assert "numero" in response.text
    assert "boe_id" in response.text


def test_fields_template_desconocida_devuelve_404(client):
    assert client.get("/fields/template/inventada").status_code == 404


def test_index_lista_las_plantillas_y_la_nota_de_privacidad(client):
    # El HTML parte las frases en varias líneas: se comparan sin esos saltos.
    body = " ".join(client.get("/").text.split())

    assert "Ley / BOE" in body
    assert "Factura" in body
    # La promesa de no almacenamiento tiene que estar visible en la página.
    assert "Nada se guarda" in body
    assert "se borran al terminar la conversión" in body


def test_convert_schema_con_varias_reglas(client, make_pdf):
    """El formulario manda campos repetidos; deben emparejarse por posición."""
    path = make_pdf("ley.pdf", [[("Ley 12/2023 BOE-A-2023-12203", 11, False)]])

    response = client.post(
        "/convert",
        data={
            "mode": "schema",
            "rule_name": ["numero", "boe_id", "ausente"],
            "rule_kind": ["regex", "regex", "label"],
            "rule_pattern": [
                r"Ley\s+(\d+/\d{4})",
                r"(BOE-[A-Z]-\d{4}-\d+)",
                "No aparece",
            ],
        },
        files=[upload(path)],
    )

    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["fields"] == {
        "numero": "12/2023",
        "boe_id": "BOE-A-2023-12203",
        "ausente": None,
    }
    assert body["unmatched"] == ["ausente"]
    assert not tmp_dirs()


def test_los_mensajes_de_error_escapan_la_entrada_del_usuario(client, make_pdf):
    """El modo y el nombre de la regla se devuelven en el HTML de error: XSS si no."""
    path = make_pdf("uno.pdf", [[("texto", 11, False)]])
    ataque = "<script>alert(1)</script>"
    navegador = {"accept": "text/html"}

    respuesta_modo = client.post(
        "/convert", data={"mode": ataque}, files=[upload(path)], headers=navegador
    )

    assert respuesta_modo.status_code == 422
    assert "<script>" not in respuesta_modo.text
    assert "&lt;script&gt;" in respuesta_modo.text

    app_module.limiter.reset()
    respuesta_regla = client.post(
        "/convert",
        data={
            "mode": "schema",
            "rule_name": ataque,
            "rule_kind": "regex",
            "rule_pattern": "(sin cerrar",
        },
        files=[upload(path)],
        headers=navegador,
    )

    assert respuesta_regla.status_code == 422
    assert "<script>" not in respuesta_regla.text


def test_el_error_en_json_no_se_puede_interpretar_como_html(client, make_pdf):
    """Un payload JSON con nosniff no lo renderiza ningún navegador."""
    path = make_pdf("uno.pdf", [[("texto", 11, False)]])

    respuesta = client.post(
        "/convert",
        data={"mode": "<script>alert(1)</script>"},
        files=[upload(path)],
        headers={"accept": "application/json"},
    )

    assert respuesta.headers["content-type"].startswith("application/json")
    assert respuesta.headers["x-content-type-options"] == "nosniff"
    assert "<script>" in respuesta.json()["error"]  # dato, no marcado


def test_la_pagina_carga_el_script_de_descarga(client):
    """Sin app.js, el navegador reintenta la descarga con GET /convert y da 405.

    La conversión solo existe en la respuesta del POST, así que ese reintento no
    encuentra nada: la descarga tiene que hacerse desde el blob, en el cliente.
    """
    assert '/static/app.js' in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_convert_no_responde_a_get(client):
    """Deja constancia del 405 que provocaba el fallo: /convert es solo POST."""
    assert client.get("/convert").status_code == 405


def test_todas_las_paginas_legales_responden(client):
    for pagina in app_module.PAGINAS_LEGALES:
        respuesta = client.get(f"/legal/{pagina}")

        assert respuesta.status_code == 200, pagina
        assert "text/html" in respuesta.headers["content-type"]


def test_pagina_legal_desconocida_devuelve_404(client):
    assert client.get("/legal/inventada").status_code == 404


def test_las_legales_identifican_al_titular(client):
    """Sin titular ni correo de contacto, nadie puede ejercer sus derechos."""
    for pagina in ("aviso-legal", "privacidad"):
        texto = client.get(f"/legal/{pagina}").text

        assert app_module.TITULAR["nombre"] in texto, pagina
        assert app_module.TITULAR["email"] in texto, pagina


def test_el_titular_se_puede_cambiar_por_entorno(client, monkeypatch):
    monkeypatch.setitem(app_module.TITULAR, "nombre", "Otro Titular")
    monkeypatch.setitem(app_module.TITULAR, "email", "contacto@ejemplo.test")

    texto = client.get("/legal/privacidad").text

    assert "Otro Titular" in texto
    assert "contacto@ejemplo.test" in texto


def test_las_condiciones_muestran_los_limites_reales(client, monkeypatch):
    """Si los límites cambian por entorno, las condiciones no pueden mentir."""
    monkeypatch.setattr(app_module, "MAX_FILES", 3)

    texto = " ".join(client.get("/legal/condiciones").text.split())

    assert "3 ficheros como máximo" in texto


def test_la_barra_y_el_pie_estan_en_todas_las_paginas(client):
    rutas = ["/", "/legal/aviso-legal", "/legal/privacidad", "/legal/condiciones"]

    for ruta in rutas:
        texto = client.get(ruta).text

        assert "TecXarT" in texto, ruta
        assert "https://www.tecxart.es" in texto, ruta
        assert "https://github.com/tecxion/pdf-a-json" in texto, ruta
        assert "/legal/privacidad" in texto, ruta


def test_convert_modo_auto(client, make_pdf):
    path = make_pdf(
        "ley.pdf",
        [
            [
                ("CAPÍTULO I", 16, True),
                ("Artículo 1. Objeto.", 14, True),
                ("Esta ley regula el asunto.", 11, False),
                ("Artículo 2. Ámbito.", 14, True),
            ]
        ],
    )

    response = client.post("/convert", data={"mode": "auto"}, files=[upload(path)])

    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["mode"] == "layout"
    assert body["summary"]["auto"]["elegido"] == "layout"
    assert not tmp_dirs()


def test_el_modo_por_defecto_es_auto(client, make_pdf):
    """Sin indicar modo, el formulario y la API usan el automático."""
    path = make_pdf("carta.pdf", [[("Texto corrido sin títulos.", 11, False)]])

    response = client.post("/convert", files=[upload(path)])

    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["summary"]["auto"]["elegido"] == "raw"
    assert not tmp_dirs()


def test_una_peticion_enorme_se_corta_antes_de_leer_el_cuerpo(client, monkeypatch):
    """Starlette vuelca las subidas a /tmp al parsear: hay que cortar antes."""
    monkeypatch.setattr(app_module, "MAX_REQUEST_BYTES", 1024)

    respuesta = client.post(
        "/convert",
        data={"mode": "raw"},
        files=[("files", ("grande.pdf", b"%PDF-1.7" + b"x" * 5000, "application/pdf"))],
    )

    assert respuesta.status_code == 413
    assert "MB" in respuesta.text
    assert not tmp_dirs()


def test_sin_plazas_libres_responde_503_y_no_cuelga(client, make_pdf):
    path = make_pdf("uno.pdf", [[("A", 11, False)]])
    ocupadas = [app_module._plazas.acquire(blocking=False) for _ in range(app_module.MAX_CONCURRENTES)]
    assert all(ocupadas)

    try:
        respuesta = client.post("/convert", data={"mode": "raw"}, files=[upload(path)])
    finally:
        for _ in ocupadas:
            app_module._plazas.release()

    assert respuesta.status_code == 503
    assert "minuto" in respuesta.text
    assert not tmp_dirs()


def test_la_plaza_se_libera_aunque_la_conversion_falle(client, tmp_path):
    """Un semáforo que no se libera deja el servicio muerto tras unos errores."""
    roto = tmp_path / "roto.pdf"
    roto.write_bytes(b"%PDF-1.7\nbasura\n")

    for _ in range(app_module.MAX_CONCURRENTES + 2):
        app_module.limiter.reset()
        assert client.post("/convert", data={"mode": "raw"}, files=[upload(roto)]).status_code == 422

    assert app_module._plazas.acquire(blocking=False)
    app_module._plazas.release()


def test_health_comprueba_una_conversion_real(client):
    respuesta = client.get("/health")

    assert respuesta.status_code == 200
    assert respuesta.json() == {"status": "ok"}
    assert not tmp_dirs()


def test_health_avisa_cuando_la_conversion_esta_rota(client, monkeypatch):
    def pool_roto(*_args, **_kwargs):
        raise RuntimeError("pool caído")

    monkeypatch.setattr(app_module, "_run_conversion", pool_roto)

    respuesta = client.get("/health")

    assert respuesta.status_code == 503
    assert respuesta.json() == {"status": "degraded"}
    assert not tmp_dirs()


def test_cabeceras_de_seguridad_en_todas_las_respuestas(client):
    for ruta in ("/", "/legal/privacidad", "/health"):
        cabeceras = client.get(ruta).headers

        assert cabeceras["x-content-type-options"] == "nosniff"
        assert cabeceras["referrer-policy"] == "no-referrer"
        assert cabeceras["x-frame-options"] == "DENY"
        assert "default-src 'self'" in cabeceras["content-security-policy"]


def test_el_favicon_existe_y_la_pagina_lo_enlaza(client):
    assert "/static/favicon.svg" in client.get("/").text
    respuesta = client.get("/static/favicon.svg")
    assert respuesta.status_code == 200
    assert "svg" in respuesta.headers["content-type"]


def test_un_pdf_sin_texto_da_error_claro_y_no_un_json_vacio(client, make_pdf):
    """Devolver páginas vacías es un fallo silencioso: nadie entiende qué pasó."""
    path = make_pdf("escaneado.pdf", [[], []])

    respuesta = client.post("/convert", data={"mode": "auto"}, files=[upload(path)])

    assert respuesta.status_code == 422
    assert "escaneado como imagen" in respuesta.text
    assert "no hace reconocimiento óptico" in respuesta.text
    assert not tmp_dirs()


def test_con_ocr_activado_el_mensaje_cambia(client, make_pdf, monkeypatch):
    monkeypatch.setattr(app_module, "OCR_ENABLED", True)
    path = make_pdf("escaneado.pdf", [[]])

    respuesta = client.post("/convert", data={"mode": "raw"}, files=[upload(path)])

    assert respuesta.status_code == 422
    assert "no pudo leerlo" in respuesta.text
    assert not tmp_dirs()


def test_un_lote_con_un_pdf_escaneado_convierte_el_resto(client, make_pdf):
    bueno = make_pdf("bueno.pdf", [[("Con texto", 11, False)]])
    escaneado = make_pdf("escaneado.pdf", [[]])

    respuesta = client.post(
        "/convert", data={"mode": "raw"}, files=[upload(bueno), upload(escaneado)]
    )

    assert respuesta.status_code == 200
    with zipfile.ZipFile(io.BytesIO(respuesta.content)) as archivo:
        assert "bueno.json" in archivo.namelist()
        errores = json.loads(archivo.read("_errors.json"))
        assert errores[0]["filename"] == "escaneado.pdf"
        assert "escaneado como imagen" in errores[0]["error"]
    assert not tmp_dirs()


def test_un_cliente_de_api_recibe_los_errores_en_json(client, tmp_path):
    """Un script que recibe HTML no puede saber qué ha fallado."""
    roto = tmp_path / "roto.pdf"
    roto.write_bytes(b"no soy un pdf")

    respuesta = client.post(
        "/convert",
        data={"mode": "raw"},
        files=[upload(roto)],
        headers={"accept": "application/json"},
    )

    assert respuesta.status_code == 422
    assert respuesta.headers["content-type"].startswith("application/json")
    assert "no es un PDF" in respuesta.json()["error"]


def test_sin_cabecera_accept_tambien_responde_json(client):
    """curl no manda Accept: text/html, así que no debería recibir una página."""
    respuesta = client.post("/convert", data={"mode": "inventado"}, headers={"accept": "*/*"})

    assert respuesta.status_code == 422
    assert respuesta.json()["error"].startswith("Modo desconocido")


def test_un_navegador_sigue_recibiendo_la_pagina_de_error(client):
    respuesta = client.post(
        "/convert",
        data={"mode": "inventado"},
        headers={"accept": "text/html,application/xhtml+xml"},
    )

    assert respuesta.status_code == 422
    assert "text/html" in respuesta.headers["content-type"]
    assert "No se pudo" in respuesta.text


def test_la_documentacion_de_la_api_esta_publicada(client):
    esquema = client.get("/openapi.json")

    assert esquema.status_code == 200
    assert "/convert" in esquema.json()["paths"]
    assert client.get("/docs").status_code == 200


def test_la_portada_trae_el_panel_de_resultado(client):
    cuerpo = client.get("/").text

    assert 'id="resultado"' in cuerpo
    assert "/static/app.js" in cuerpo


def test_reglas_texto_devuelve_el_texto_del_pdf(client, make_pdf):
    path = make_pdf("ley.pdf", [[("Ley 12/2023, de prueba.", 11, False)]])

    respuesta = client.post("/reglas/texto", files=[upload(path)])

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert "Ley 12/2023" in cuerpo["texto"]
    assert cuerpo["truncado"] is False
    assert not tmp_dirs()


def test_reglas_texto_rechaza_un_pdf_escaneado(client, make_pdf):
    path = make_pdf("escaneado.pdf", [[]])

    respuesta = client.post("/reglas/texto", files=[upload(path)])

    assert respuesta.status_code == 422
    assert "escaneado" in respuesta.json()["error"]
    assert not tmp_dirs()


def test_reglas_texto_sin_fichero(client):
    assert client.post("/reglas/texto").status_code == 422


def test_probar_reglas_dice_que_captura_cada_una(client):
    respuesta = client.post(
        "/reglas/probar",
        data={
            "texto": "Boletin oficial. Ley 12/2023, de 24 de mayo. Fin.",
            "rule_name": ["numero", "ausente"],
            "rule_kind": ["regex", "label"],
            "rule_pattern": [r"Ley\s+(\d+/\d{4})", "No aparece"],
        },
    )

    assert respuesta.status_code == 200
    resultados = respuesta.json()["resultados"]
    assert resultados[0]["valor"] == "12/2023"
    assert resultados[0]["encontrado"] is True
    assert "Ley 12/2023" in resultados[0]["contexto"]
    assert resultados[1]["encontrado"] is False
    assert resultados[1]["valor"] is None


def test_probar_reglas_rechaza_un_patron_invalido(client):
    respuesta = client.post(
        "/reglas/probar",
        data={
            "texto": "algo",
            "rule_name": ["x"],
            "rule_kind": ["regex"],
            "rule_pattern": ["(sin cerrar"],
        },
    )

    assert respuesta.status_code == 422
    assert "inválido" in respuesta.json()["error"]


def test_probar_reglas_sin_texto_o_sin_reglas(client):
    assert client.post("/reglas/probar", data={"texto": "  "}).status_code == 422
    assert client.post("/reglas/probar", data={"texto": "algo"}).status_code == 422


def test_probar_una_regla_catastrofica_no_cuelga_el_servidor(client):
    respuesta = client.post(
        "/reglas/probar",
        data={
            "texto": "a" * 34 + "b",
            "rule_name": ["bomba"],
            "rule_kind": ["regex"],
            "rule_pattern": [r"(a|aa)+$"],
        },
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["resultados"][0]["encontrado"] is False
    assert "abortó" in respuesta.json()["resultados"][0]["error"]
