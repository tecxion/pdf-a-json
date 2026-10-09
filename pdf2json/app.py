"""Capa web. No importa pymupdf: todo lo que toca el PDF vive en extract/modes.

Promesa de no almacenamiento: cada petición trabaja en su propio directorio
tempfile con prefijo TMP_PREFIX, borrado en `finally`.
"""
from __future__ import annotations

import base64
import glob
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import modes
from .errors import PdfError, SinTexto, TooManyPages
from .templates_rules import TEMPLATES

log = logging.getLogger("pdf2json")

MAX_FILE_BYTES = int(os.getenv("MAX_FILE_MB", "25")) * 1024 * 1024
MAX_FILES = int(os.getenv("MAX_FILES", "10"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "500"))
RATE_LIMIT = os.getenv("RATE_LIMIT", "10/minute")
CONVERT_TIMEOUT_S = int(os.getenv("CONVERT_TIMEOUT_S", "30"))
# Tope de la petición entera. No basta con el límite por fichero: Starlette
# vuelca cada subida de más de 1 MB a /tmp mientras parsea el formulario, o sea
# antes de que este código llegue a validar nada. Con /tmp montado en tmpfs eso
# es RAM del servidor, así que el corte tiene que ocurrir antes de leer el cuerpo.
MAX_REQUEST_MB = int(os.getenv("MAX_REQUEST_MB", "60"))
MAX_REQUEST_BYTES = MAX_REQUEST_MB * 1024 * 1024
# Conversiones simultáneas. Por encima se rechaza con 503 en vez de encolar
# peticiones que acabarían todas en timeout.
MAX_CONCURRENTES = int(os.getenv("MAX_CONCURRENTES", "4"))
# OCR para páginas escaneadas. Apagado por defecto: multiplica el tiempo de
# conversión y exige Tesseract en la imagen. Si se activa sin él, se avisa en el
# registro y las páginas sin texto siguen saliendo vacías.
OCR_ENABLED = os.getenv("OCR_ENABLED", "0") == "1"
OCR_IDIOMA = os.getenv("OCR_IDIOMA", "spa")

# Identificación del titular en las páginas legales. Van por entorno para que
# quien aloje su propia instancia ponga la suya sin tocar el código.
TITULAR = {
    "nombre": os.getenv("LEGAL_TITULAR", "TecXarT"),
    "email": os.getenv("LEGAL_EMAIL", "tecxart@gmail.com"),
}

LEGAL_ACTUALIZADO = "2026-10-08"

PAGINAS_LEGALES = {
    "aviso-legal": "legal_aviso.html",
    "privacidad": "legal_privacidad.html",
    "condiciones": "legal_condiciones.html",
}

TMP_PREFIX = "pdf2json-"
CHUNK = 1 << 20
MODES = ("auto", "raw", "layout", "schema")

_plazas = threading.BoundedSemaphore(MAX_CONCURRENTES)

# PDF mínimo de una página, para que /health compruebe una conversión real.
# Va incrustado porque app.py no puede importar pymupdf para fabricarlo.
_PDF_SONDA = base64.b64decode(
    "JVBERi0xLjcKJcK1wrYKJSBXcml0dGVuIGJ5IE11UERGIDEuMjguMgoKMSAwIG9iago8PC9UeXBl"
    "L0NhdGFsb2cvUGFnZXMgMiAwIFIvSW5mbzw8L1Byb2R1Y2VyKE11UERGIDEuMjguMik+Pj4+CmVu"
    "ZG9iagoKMiAwIG9iago8PC9UeXBlL1BhZ2VzL0NvdW50IDEvS2lkc1s0IDAgUl0+PgplbmRvYmoK"
    "CjMgMCBvYmoKPDwvRm9udDw8L2hlbHYgNSAwIFI+Pj4+CmVuZG9iagoKNCAwIG9iago8PC9UeXBl"
    "L1BhZ2UvTWVkaWFCb3hbMCAwIDcyIDcyXS9Sb3RhdGUgMC9SZXNvdXJjZXMgMyAwIFIvUGFyZW50"
    "IDIgMCBSL0NvbnRlbnRzWzYgMCBSXT4+CmVuZG9iagoKNSAwIG9iago8PC9UeXBlL0ZvbnQvU3Vi"
    "dHlwZS9UeXBlMS9CYXNlRm9udC9IZWx2ZXRpY2EvRW5jb2RpbmcvV2luQW5zaUVuY29kaW5nPj4K"
    "ZW5kb2JqCgo2IDAgb2JqCjw8L0xlbmd0aCA1Ni9GaWx0ZXIvRmxhdGVEZWNvZGU+PgpzdHJlYW0K"
    "eJzjKuRyCuEyVDAAQkMFQwMFUyOFkFwu/YzUnDIFC4WQNIVoG7M0syS72BAvLtcQrkAuABFIC3cK"
    "ZW5kc3RyZWFtCmVuZG9iagoKeHJlZgowIDcKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDQy"
    "IDAwMDAwIG4gCjAwMDAwMDAxMjAgMDAwMDAgbiAKMDAwMDAwMDE3MiAwMDAwMCBuIAowMDAwMDAw"
    "MjEzIDAwMDAwIG4gCjAwMDAwMDAzMTggMDAwMDAgbiAKMDAwMDAwMDQwNyAwMDAwMCBuIAoKdHJh"
    "aWxlcgo8PC9TaXplIDcvUm9vdCAxIDAgUi9JRFs8QzJCQTUzNkJDMkE3NjUyRDUyQzJBRDE3MTYz"
    "OEMyQTI+PEYwQjM0OUNCMDY5NDMyMUNCNzgwNkJEQjI3RDQyMzBEPl0+PgpzdGFydHhyZWYKNTMx"
    "CiUlRU9GCg=="
)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
limiter = Limiter(key_func=get_remote_address)


class ConvertError(Exception):
    """Error que se muestra al usuario tal cual."""

    status = 422


class FileTooLarge(ConvertError):
    status = 413


class TooManyFiles(ConvertError):
    status = 413


class NotPdf(ConvertError):
    status = 422


class ConvertTimeout(ConvertError):
    status = 504


class Ocupado(ConvertError):
    status = 503


class PeticionDemasiadoGrande(ConvertError):
    status = 413


# --- pool de procesos ---------------------------------------------------------
# Hace falta un proceso y no un hilo: un hilo bloqueado en el C de PyMuPDF no se
# puede interrumpir desde Python, y un PDF patológico colgaría el worker.

_pool: ProcessPoolExecutor | None = None


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=2)
    return _pool


def _reset_pool() -> None:
    global _pool
    if _pool is not None:
        # shutdown(wait=False) deja de aceptar trabajo pero NO interrumpe al worker
        # que ya está corriendo: un PDF patológico seguiría quemando CPU hasta
        # terminar. Hay que matarlo a mano.
        # ponytail: _processes es privado de ProcessPoolExecutor. El getattr deja
        # que una versión futura de Python que lo renombre degrade al comportamiento
        # anterior (worker abandonado) en vez de romper la petición.
        workers = list(getattr(_pool, "_processes", {}).values())
        _pool.shutdown(wait=False, cancel_futures=True)
        for worker in workers:
            if worker.is_alive():
                worker.kill()
    _pool = None


def _convert_file(
    path: str,
    mode: str,
    rules: list[dict],
    max_pages: int,
    ocr: bool = False,
    ocr_idioma: str = "spa",
) -> dict:
    """Corre en el proceso worker. Debe ser picklable: nivel de módulo, sin cierres."""
    from . import extract  # aquí dentro para que app.py no dependa de pymupdf

    pages = extract.page_count(path)
    if pages > max_pages:
        raise TooManyPages(
            f"El documento tiene {pages} páginas y el límite es {max_pages}."
        )

    # auto necesita las tablas igual que layout: puede acabar devolviéndolo.
    doc = extract.parse(
        path,
        with_tables=(mode in ("auto", "layout")),
        ocr=ocr,
        ocr_idioma=ocr_idioma,
    )

    # Un JSON con todas las páginas vacías no es un resultado, es un fallo
    # silencioso: quien lo recibe no entiende por qué no hay nada dentro.
    if not any(page.has_text for page in doc.pages):
        raise SinTexto(
            "Este PDF no tiene texto: está escaneado como imagen. "
            + (
                "El reconocimiento óptico no pudo leerlo."
                if ocr
                else "Esta herramienta no hace reconocimiento óptico."
            )
        )
    if mode == "auto":
        return modes.auto(doc)
    if mode == "raw":
        return modes.raw(doc)
    if mode == "layout":
        return modes.layout(doc)
    return modes.schema(doc, modes.compile_rules(rules))


def _run_conversion(path: Path, mode: str, rules: list[dict]) -> dict:
    future = _get_pool().submit(
        _convert_file, str(path), mode, rules, MAX_PAGES, OCR_ENABLED, OCR_IDIOMA
    )
    try:
        return future.result(timeout=CONVERT_TIMEOUT_S)
    except FutureTimeout as exc:
        _reset_pool()  # mata el proceso colgado
        raise ConvertTimeout(
            f"La conversión superó {CONVERT_TIMEOUT_S}s y se canceló."
        ) from exc


@contextmanager
def _plaza_de_conversion():
    """Limita las conversiones simultáneas.

    Sin esto, con el pool lleno las peticiones se encolan y acaban todas en
    timeout, incluida la primera que llegó. Es más honesto decir que no hay
    sitio ahora mismo.
    """
    if not _plazas.acquire(blocking=False):
        raise Ocupado(
            "El servidor está convirtiendo todo lo que puede ahora mismo. "
            "Inténtalo de nuevo en un minuto."
        )
    try:
        yield
    finally:
        _plazas.release()


# --- utilidades ---------------------------------------------------------------


def _safe_name(name: str | None, ext: str) -> str:
    """Nombre de descarga seguro: Path().stem descarta cualquier parte de ruta."""
    stem = Path(name or "documento").stem
    # El strip evita nombres hechos solo de relleno ("   " -> "___") y los puntos
    # iniciales de un intento de travesía.
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem).strip("._-")[:80]
    return f"{stem or 'documento'}{ext}"


def _save_upload(upload: UploadFile, dest: Path) -> None:
    """Guarda la subida comprobando el tamaño real, no el Content-Length."""
    total = 0
    with dest.open("wb") as out:
        while chunk := upload.file.read(CHUNK):
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise FileTooLarge(
                    f"Un fichero supera el límite de "
                    f"{MAX_FILE_BYTES // (1024 * 1024)} MB."
                )
            out.write(chunk)
    with dest.open("rb") as handle:
        if b"%PDF" not in handle.read(1024):
            raise NotPdf("El fichero no es un PDF.")


def _parse_rules(names, kinds, patterns) -> list[dict]:
    rules = []
    for name, kind, pattern in zip(names, kinds, patterns):
        if not (name or "").strip() and not (pattern or "").strip():
            continue  # fila vacía del formulario
        rules.append({"name": name, "kind": kind, "pattern": pattern})
    return rules


def _describe(exc: Exception) -> tuple[str, int]:
    """Traduce una excepción a (mensaje para el usuario, código HTTP)."""
    if isinstance(exc, TooManyPages):
        return str(exc), 413
    if isinstance(exc, SinTexto):
        return str(exc), 422
    if isinstance(exc, PdfError):  # EncryptedPdf, CorruptPdf y futuras
        return str(exc), 422
    log.exception("fallo inesperado en la conversión")  # sin nombre ni contenido
    return "Error inesperado al convertir el PDF.", 500


def _quiere_json(request: Request) -> bool:
    """Un cliente de API pide JSON; un navegador pide HTML.

    Sin esto, un script que recibe un 422 se encuentra una página entera de HTML
    y no puede saber qué falló.
    """
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return True
    return "text/html" not in accept


def _error(request: Request, message: str, status: int) -> Response:
    if _quiere_json(request):
        return JSONResponse({"error": message}, status_code=status)
    return templates.TemplateResponse(
        request, "error.html", {"message": message}, status_code=status
    )


def _json_response(payload: dict, filename: str) -> Response:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=body,
        media_type="application/json",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


def _zip_response(results: list[tuple[str, dict]], errors: list[dict]) -> Response:
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for original, payload in results:
            name = _safe_name(original, ".json")
            if name in used:  # dos subidas con el mismo nombre
                stem = Path(name).stem
                index = 2
                while f"{stem}_{index}.json" in used:
                    index += 1
                name = f"{stem}_{index}.json"
            used.add(name)
            archive.writestr(name, json.dumps(payload, ensure_ascii=False, indent=2))
        if errors:
            archive.writestr(
                "_errors.json", json.dumps(errors, ensure_ascii=False, indent=2)
            )
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"content-disposition": 'attachment; filename="pdf2json.zip"'},
    )


# --- aplicación ---------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Barrido de huérfanos: lo que dejara un proceso muerto por SIGKILL o por OOM.
    for stale in glob.glob(os.path.join(tempfile.gettempdir(), TMP_PREFIX + "*")):
        shutil.rmtree(stale, ignore_errors=True)
    yield
    _reset_pool()


app = FastAPI(title="pdf2json", lifespan=lifespan)
app.state.limiter = limiter


@app.middleware("http")
async def _guardar_el_servidor(request: Request, call_next):
    """Corta las peticiones enormes antes de leer el cuerpo y pone cabeceras.

    El orden importa: si se deja pasar a Starlette, para cuando el endpoint se
    ejecuta las subidas ya están escritas en /tmp.
    """
    longitud = request.headers.get("content-length")
    if longitud and longitud.isdigit() and int(longitud) > MAX_REQUEST_BYTES:
        respuesta = _error(
            request,
            f"La petición entera supera {MAX_REQUEST_MB} MB. Sube menos ficheros de una vez.",
            PeticionDemasiadoGrande.status,
        )
    else:
        respuesta = await call_next(request)

    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["Referrer-Policy"] = "no-referrer"
    respuesta.headers["X-Frame-Options"] = "DENY"
    # Todo se sirve desde este origen: ni scripts, ni fuentes, ni conexiones fuera.
    respuesta.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self'; script-src 'self' 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    return respuesta
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.exception_handler(RateLimitExceeded)
async def _rate_limited(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        {"error": "Demasiadas peticiones. Espera un momento e inténtalo de nuevo."},
        status_code=429,
        headers={"Retry-After": "60"},
    )


@app.get("/health")
def health():
    """Comprueba que el pool convierte de verdad, no solo que el proceso vive.

    Un healthcheck que siempre dice que sí deja al contenedor roto en pie.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
    try:
        sonda = tmpdir / "sonda.pdf"
        sonda.write_bytes(_PDF_SONDA)
        resultado = _run_conversion(sonda, "raw", [])
        if resultado["metadata"]["page_count"] != 1:
            raise ValueError("la conversión de prueba no devolvió una página")
    except Exception:  # noqa: BLE001 - frontera del healthcheck
        log.exception("healthcheck: la conversión de prueba falló")
        return JSONResponse({"status": "degraded"}, status_code=503)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"templates_rules": TEMPLATES, "max_files": MAX_FILES}
    )


@app.get("/legal/{pagina}", response_class=HTMLResponse)
def legal(request: Request, pagina: str):
    plantilla = PAGINAS_LEGALES.get(pagina)
    if plantilla is None:
        return _error(request, f"Página legal desconocida: {pagina}.", 404)
    return templates.TemplateResponse(
        request,
        plantilla,
        {
            "titular": TITULAR,
            "actualizado": LEGAL_ACTUALIZADO,
            "limites": {
                "max_file_mb": MAX_FILE_BYTES // (1024 * 1024),
                "max_files": MAX_FILES,
                "max_pages": MAX_PAGES,
                "rate_limit": RATE_LIMIT,
                "timeout": CONVERT_TIMEOUT_S,
            },
        },
    )


@app.post("/convert")
@limiter.limit(RATE_LIMIT)
def convert(
    request: Request,
    files: list[UploadFile] = File(default=[]),
    mode: str = Form("auto"),
    rule_name: list[str] = Form(default=[]),
    rule_kind: list[str] = Form(default=[]),
    rule_pattern: list[str] = Form(default=[]),
):
    # Endpoint síncrono a propósito: FastAPI lo corre en su threadpool, así la
    # espera bloqueante del pool de procesos no para el event loop.
    if mode not in MODES:
        return _error(request, f"Modo desconocido: {mode}.", 422)
    if not files or all(not f.filename for f in files):
        return _error(request, "No has seleccionado ningún PDF.", 422)
    if len(files) > MAX_FILES:
        return _error(
            request, f"Máximo {MAX_FILES} ficheros por petición.", TooManyFiles.status
        )

    rules = _parse_rules(rule_name, rule_kind, rule_pattern)
    if mode == "schema":
        if not rules:
            return _error(
                request, "El modo de campos necesita al menos una regla.", 422
            )
        try:
            modes.compile_rules(rules)  # validación antes de tocar el PDF
        except modes.RuleError as exc:
            return _error(request, str(exc), 422)

    tmpdir = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
    try:
        results: list[tuple[str, dict]] = []
        errors: list[dict] = []

        for index, upload in enumerate(files):
            source = tmpdir / f"{index}.pdf"
            try:
                _save_upload(upload, source)
                with _plaza_de_conversion():
                    results.append(
                        (upload.filename, _run_conversion(source, mode, rules))
                    )
            except ConvertError as exc:
                if len(files) == 1:
                    return _error(request, str(exc), exc.status)
                errors.append({"filename": upload.filename, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - frontera por fichero
                message, status = _describe(exc)
                if len(files) == 1:
                    return _error(request, message, status)
                errors.append({"filename": upload.filename, "error": message})
            finally:
                source.unlink(missing_ok=True)  # borra cuanto antes, no al final

        if len(files) == 1 and results:
            original, payload = results[0]
            return _json_response(payload, _safe_name(original, ".json"))
        return _zip_response(results, errors)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.get("/fields/row", response_class=HTMLResponse)
def fields_row(request: Request):
    return templates.TemplateResponse(
        request,
        "_fields.html",
        {"rules": [{"name": "", "kind": "regex", "pattern": ""}]},
    )


@app.get("/fields/template/{name}", response_class=HTMLResponse)
def fields_template(request: Request, name: str):
    template = TEMPLATES.get(name)
    if template is None:
        return _error(request, f"Plantilla desconocida: {name}.", 404)
    return templates.TemplateResponse(
        request, "_fields.html", {"rules": template["rules"]}
    )
