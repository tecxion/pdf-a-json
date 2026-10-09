"""Capa web. No importa pymupdf: todo lo que toca el PDF vive en extract/modes.

Promesa de no almacenamiento: cada petición trabaja en su propio directorio
tempfile con prefijo TMP_PREFIX, borrado en `finally`.
"""
from __future__ import annotations

import glob
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import modes
from .errors import PdfError, TooManyPages
from .templates_rules import TEMPLATES

log = logging.getLogger("pdf2json")

MAX_FILE_BYTES = int(os.getenv("MAX_FILE_MB", "25")) * 1024 * 1024
MAX_FILES = int(os.getenv("MAX_FILES", "10"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "500"))
RATE_LIMIT = os.getenv("RATE_LIMIT", "10/minute")
CONVERT_TIMEOUT_S = int(os.getenv("CONVERT_TIMEOUT_S", "30"))

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
MODES = ("raw", "layout", "schema")

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


def _convert_file(path: str, mode: str, rules: list[dict], max_pages: int) -> dict:
    """Corre en el proceso worker. Debe ser picklable: nivel de módulo, sin cierres."""
    from . import extract  # aquí dentro para que app.py no dependa de pymupdf

    pages = extract.page_count(path)
    if pages > max_pages:
        raise TooManyPages(
            f"El documento tiene {pages} páginas y el límite es {max_pages}."
        )

    doc = extract.parse(path, with_tables=(mode == "layout"))
    if mode == "raw":
        return modes.raw(doc)
    if mode == "layout":
        return modes.layout(doc)
    return modes.schema(doc, modes.compile_rules(rules))


def _run_conversion(path: Path, mode: str, rules: list[dict]) -> dict:
    future = _get_pool().submit(_convert_file, str(path), mode, rules, MAX_PAGES)
    try:
        return future.result(timeout=CONVERT_TIMEOUT_S)
    except FutureTimeout as exc:
        _reset_pool()  # mata el proceso colgado
        raise ConvertTimeout(
            f"La conversión superó {CONVERT_TIMEOUT_S}s y se canceló."
        ) from exc


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
    if isinstance(exc, PdfError):  # EncryptedPdf, CorruptPdf y futuras
        return str(exc), 422
    log.exception("fallo inesperado en la conversión")  # sin nombre ni contenido
    return "Error inesperado al convertir el PDF.", 500


def _error(request: Request, message: str, status: int) -> HTMLResponse:
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
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.exception_handler(RateLimitExceeded)
async def _rate_limited(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        {"error": "Demasiadas peticiones. Espera un momento e inténtalo de nuevo."},
        status_code=429,
        headers={"Retry-After": "60"},
    )


@app.get("/health")
def health() -> dict:
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
    mode: str = Form("raw"),
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
                results.append((upload.filename, _run_conversion(source, mode, rules)))
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
