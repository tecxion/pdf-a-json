# pdf2json — diseño

Fecha: 2026-10-07
Estado: aprobado

## 1. Resumen

Aplicación web de código abierto que convierte PDF a JSON. El usuario sube uno o varios
PDF, elige un modo de conversión y descarga el JSON (o un ZIP si son varios). El servidor
no conserva ningún PDF ni JSON: los ficheros temporales se borran al terminar cada
petición.

Backend y lógica en Python. Frontend: plantillas Jinja servidas por FastAPI, con HTMX
para las partes dinámicas. Sin base de datos, sin cuentas de usuario, sin estado entre
peticiones.

## 2. Objetivos

- Convertir cualquier PDF con capa de texto a JSON, de forma fiable y rápida.
- Tres modos de salida elegibles desde la web: `raw`, `layout`, `schema`.
- Uso público anónimo en un VPS pequeño sin riesgo de que un usuario sature el servicio.
- Núcleo reutilizable: la lógica de conversión no depende de la capa web.
- Caso de uso principal del autor: documentos legales (leyes, BOE). El núcleo es genérico;
  el soporte legal son heurísticas y plantillas añadidas, no un acoplamiento.

## 3. No objetivos (v1)

- OCR de PDF escaneados. Se detecta la ausencia de texto y se avisa, no se resuelve.
- Extracción con LLM o cualquier llamada a servicios externos.
- Cuentas, historial, API con claves, procesamiento en cola o asíncrono.
- Conversión a formatos distintos de JSON.

## 4. Stack y licencia

| Pieza | Elección |
| --- | --- |
| Parseo PDF | PyMuPDF (`pymupdf`) |
| Web | FastAPI + Uvicorn |
| Plantillas | Jinja2 |
| Interactividad | HTMX (fichero estático servido localmente, sin CDN) |
| Regex con timeout | `regex` |
| Rate limit | `slowapi` |
| Tests | pytest |
| Licencia del proyecto | **AGPL-3.0** |

PyMuPDF es AGPL-3.0, por lo que el proyecto completo se publica como AGPL-3.0. Decisión
tomada a cambio de su velocidad (cientos de páginas en segundos) y de los datos de
layout que expone (bloques con tamaño de fuente, peso y posición, y detección de tablas).

## 5. Estructura de ficheros

```
pdf2json/
  __init__.py
  extract.py          # PDF -> modelo de documento interno
  modes.py            # raw() | layout() | schema()  -> dict
  templates_rules.py  # plantillas de reglas: ley/BOE, factura, CV
  app.py              # FastAPI: rutas, validación, límites, temporales
  templates/
    index.html
    error.html
    _fields.html      # fragmento HTMX: fila de campo del modo schema
  static/
    app.css
    htmx.min.js
tests/
  test_extract.py
  test_modes.py
  test_app.py
Dockerfile
docker-compose.yml
pyproject.toml
LICENSE
README.md
```

`extract.py` y `modes.py` no importan nada de FastAPI. `app.py` no importa `pymupdf`.
El parseo del PDF ocurre una sola vez por fichero; los tres modos consumen el mismo
modelo interno.

## 6. Modelo de documento interno

Producido por `extract.parse(path) -> Doc`. Dataclasses:

```python
@dataclass
class Block:
    text: str
    size: float        # tamaño de fuente dominante del bloque
    bold: bool
    bbox: tuple[float, float, float, float]

@dataclass
class Table:
    page: int
    rows: list[list[str]]

@dataclass
class Page:
    number: int        # 1-indexado
    blocks: list[Block]
    tables: list[Table]
    has_text: bool     # False si la página no tiene ningún carácter extraíble

@dataclass
class Doc:
    metadata: dict     # title, author, subject, creator, producer,
                       # creation_date, mod_date, page_count, encrypted
    pages: list[Page]
```

`has_text=False` es la señal de "probablemente escaneada". Se calcula por página, no por
documento, porque los PDF legales mezclan páginas digitales con anexos escaneados.

## 7. Modos de conversión

Todas las salidas llevan `schema_version`, `mode`, `metadata` y `summary`. `summary`
siempre incluye `pages_without_text`, para que el consumidor detecte páginas perdidas sin
recorrer el documento.

### 7.1 `raw`

Texto por página. Fiel al PDF, sin interpretar. No puede fallar si el PDF abre.

```json
{
  "schema_version": 1,
  "mode": "raw",
  "metadata": { "title": "...", "page_count": 42, "encrypted": false },
  "pages": [
    { "number": 1, "text": "...", "ocr_required": false }
  ],
  "summary": { "pages_without_text": [7, 8] }
}
```

### 7.2 `layout`

Jerarquía de secciones. Heurística, con una capa específica para texto legal.

Detección de títulos:

1. Se calcula la mediana del tamaño de fuente de todos los bloques (el cuerpo del texto).
2. Un bloque es candidato a título si `size > mediana * 1.15`, o si es negrita y tiene
   menos de 120 caracteres.
3. Los tamaños distintos de los candidatos se ordenan de mayor a menor; su posición en ese
   orden da el `level` (1, 2, 3...).
4. **Override legal**, aplicado antes de la heurística de fuente, sobre el texto del bloque:
   - `^(TÍTULO|CAPÍTULO|SECCIÓN|LIBRO)\b` → nivel 1
   - `^Disposición\s+(adicional|transitoria|derogatoria|final)` → nivel 1
   - `^Artículo\s+\d+` → nivel 2

Un bloque que no es título se acumula como texto de la última sección abierta.

```json
{
  "schema_version": 1,
  "mode": "layout",
  "metadata": { "...": "..." },
  "sections": [
    {
      "level": 1,
      "heading": "CAPÍTULO I",
      "text": "",
      "pages": [1],
      "tables": [],
      "children": [
        {
          "level": 2,
          "heading": "Artículo 1. Objeto.",
          "text": "Esta ley tiene por objeto...",
          "pages": [1, 2],
          "tables": [{ "page": 2, "rows": [["Concepto", "Importe"]] }],
          "children": []
        }
      ]
    }
  ],
  "summary": { "pages_without_text": [], "sections_found": 42 }
}
```

Si no se detecta ningún título, la salida es una única sección de nivel 1 con
`heading: null` y todo el texto, más `summary.sections_found: 0`. Degrada, no falla.

### 7.3 `schema`

Extracción de campos nombrados. Dos fuentes de reglas, combinables en la misma petición:

- **Reglas del usuario**, introducidas en la web. Cada una es `{name, kind, pattern}`:
  - `kind: "regex"` → patrón con al menos un grupo de captura; el valor es el grupo 1.
  - `kind: "label"` → busca el texto literal `pattern` y captura el resto de la línea.
    Pensado para quien no escribe regex.
- **Plantilla predefinida** (`templates_rules.py`), que precarga un conjunto de reglas.
  La web las muestra ya rellenas en el formulario y el usuario puede editarlas o
  borrarlas antes de enviar. Plantillas v1: `ley_boe`, `factura`, `cv`.

Las reglas se aplican sobre el texto plano completo del documento (concatenación de
páginas), con `regex.MULTILINE`.

```json
{
  "schema_version": 1,
  "mode": "schema",
  "metadata": { "...": "..." },
  "fields": { "numero_ley": "12/2023", "fecha_publicacion": null },
  "unmatched": ["fecha_publicacion"],
  "rules_applied": [
    { "name": "numero_ley", "kind": "regex", "pattern": "Ley\\s+(\\d+/\\d{4})" }
  ],
  "summary": { "pages_without_text": [] }
}
```

Un campo sin coincidencia vale `null` y aparece en `unmatched`. Nunca se omite la clave:
el consumidor obtiene siempre la misma forma.

`summary.rule_errors` solo aparece si alguna regla falló en ejecución (por timeout). Es
una lista de `{name, error}`. Si ninguna regla falla, la clave no se incluye.

**Seguridad de los regex.** Los patrones vienen de usuarios anónimos de internet, así que
son entrada no confiable y pueden colgar un worker (ReDoS). Medidas:

- Longitud máxima del patrón: 200 caracteres.
- Máximo 30 reglas por petición.
- Ejecución con la librería `regex` y `timeout=1.0` por regla; al expirar, el campo queda
  `null` y se añade a `unmatched` junto con el motivo en `summary.rule_errors`.
- Un patrón que no compila se rechaza con 422 nombrando la regla y el error de sintaxis,
  antes de abrir el PDF.

## 8. Flujo HTTP

| Ruta | Método | Función |
| --- | --- | --- |
| `/` | GET | Formulario: selector de archivos, modo, panel de reglas (modo schema) |
| `/fields/row` | GET | Fragmento HTMX: añade una fila de regla vacía |
| `/fields/template/{nombre}` | GET | Fragmento HTMX: filas precargadas de una plantilla |
| `/convert` | POST | Conversión y descarga |
| `/health` | GET | `{"status":"ok"}` para el VPS |

`/convert` es un `POST` de formulario clásico, no HTMX: la respuesta es un fichero y el
navegador la descarga directamente. HTMX se usa solo para el panel de reglas, que es donde
aporta algo. En caso de error, `/convert` devuelve `error.html` renderizado con el mensaje.

Secuencia de `/convert`:

1. Rate limit por IP (`slowapi`), antes de leer el cuerpo.
2. Crear un directorio temporal de la petición: `tempfile.mkdtemp(prefix="pdf2json-")`.
3. Por cada fichero subido: validar extensión `.pdf`, que los primeros bytes sean `%PDF`,
   y el tamaño. Escribir a `{tmpdir}/{n}.pdf` leyendo por bloques, abortando en cuanto se
   excede el límite de tamaño (no se confía en `Content-Length`).
4. Validar el número de páginas tras abrir el documento.
5. `extract.parse()` → modo elegido → `dict`.
6. Serializar. Un fichero → respuesta JSON con `Content-Disposition: attachment`. Varios →
   ZIP construido en memoria (`io.BytesIO`), un `.json` por PDF usando el nombre original
   saneado.
7. Borrar el directorio temporal completo.
8. Responder.

El nombre de fichero de la descarga se sanea (solo `[A-Za-z0-9._-]`, resto a `_`) para que
el `Content-Disposition` no se pueda manipular con el nombre del fichero subido.

## 9. Ficheros temporales y promesa de no almacenamiento

Los PDF sí pasan por disco, en `/tmp`, y se borran al finalizar la conversión. Esto es lo
que el proyecto promete y lo que hace.

Garantías de borrado, en tres capas:

1. **Por petición:** el directorio temporal se crea y se destruye con `try/finally`, de
   modo que se borra también si la conversión lanza una excepción, si el fichero está
   corrupto, si salta el timeout o si el cliente corta la conexión.
2. **Al arrancar:** barrido de directorios `pdf2json-*` huérfanos en `tempfile.gettempdir()`,
   para limpiar lo que dejara un proceso que murió por `SIGKILL` o por falta de memoria.
3. **En despliegue:** `docker-compose.yml` monta `/tmp` como `tmpfs`, así los PDF viven en
   RAM y desaparecen al reiniciar el contenedor sin tocar el disco físico. Documentado en
   el README como configuración recomendada.

Ni los PDF ni los JSON generados se guardan en ningún otro sitio. No hay base de datos ni
caché. Los logs registran método, ruta, código de estado, duración y bytes: **nunca** el
nombre del fichero subido, su contenido, los campos extraídos ni los patrones regex
enviados. El README incluye esta sección para que quien autoaloje la app sepa qué promete.

## 10. Errores

| Situación | Respuesta |
| --- | --- |
| PDF cifrado con contraseña | 422, "PDF protegido con contraseña: no se puede leer" |
| PDF corrupto o no es un PDF | 422, nombrando el fichero |
| Fichero > límite de tamaño | 413, indicando el límite configurado |
| Documento > límite de páginas | 413, indicando páginas y límite |
| Más ficheros de los permitidos | 413 |
| Regex que no compila | 422, con el nombre de la regla y el error |
| Rate limit superado | 429, con `Retry-After` |
| Timeout de conversión | 504 para un solo fichero; en lote, ese fichero falla y el resto sigue |
| Fallo inesperado | 500 genérico al usuario; traza completa solo en el log del servidor |

Página sin texto extraíble no es un error: la página sale con `text: ""` y
`ocr_required: true`, se lista en `summary.pages_without_text`, y la web muestra un aviso
tras la descarga explicando que probablemente sea un PDF escaneado y que la herramienta no
hace OCR.

**Lote con fallos parciales:** los ficheros convertidos se incluyen en el ZIP y los
fallidos se agrupan en `_errors.json` dentro del mismo ZIP, con `{filename, error}` por
cada uno. Un PDF roto entre diez no tira la petición entera.

Timeout de conversión: 30 segundos por fichero. Protege contra PDF patológicos (miles de
bloques diminutos, tablas degeneradas) que de otro modo bloquearían el proceso.

Mecanismo: la conversión corre en un `ProcessPoolExecutor` compartido de la aplicación y
se espera con `future.result(timeout=CONVERT_TIMEOUT_S)`. Hace falta un proceso, no un
hilo: un hilo bloqueado en C de PyMuPDF no se puede interrumpir desde Python, mientras que
al expirar el timeout el pool se cierra con `shutdown(cancel_futures=True)` y se recrea,
matando el proceso colgado. El borrado del directorio temporal lo hace siempre el proceso
padre, así que un worker muerto no deja ficheros atrás.

## 11. Límites

Todos por variable de entorno, con estos valores por defecto:

| Variable | Defecto |
| --- | --- |
| `MAX_FILE_MB` | 25 |
| `MAX_FILES` | 10 |
| `MAX_PAGES` | 500 |
| `MAX_RULES` | 30 |
| `RATE_LIMIT` | `10/minute` |
| `CONVERT_TIMEOUT_S` | 30 |

El rate limit de `slowapi` vive en la memoria del proceso. Con varios workers de Uvicorn
cada uno lleva su propia cuenta, de modo que el límite real se multiplica por el número de
workers. Aceptable para v1 con 1-2 workers; si hace falta precisión, `slowapi` admite
Redis como backend. Queda anotado en el código.

## 12. Tests

`pytest`, sin fixtures ni frameworks añadidos. Los PDF de prueba se generan en el propio
test con PyMuPDF (sabe escribir PDF), así que no hay binarios en el repositorio.

`test_extract.py`
- Un PDF de 3 páginas produce 3 `Page` numeradas desde 1.
- Una página sin texto da `has_text=False`; con texto, `True`.
- Un PDF cifrado lanza el error esperado.

`test_modes.py`
- `raw`: número de páginas correcto, el texto de cada página en su sitio,
  `pages_without_text` lista la página vacía.
- `layout`: un documento con "CAPÍTULO I" y "Artículo 1" produce `Artículo 1` como hijo
  nivel 2 de la sección nivel 1.
- `layout`: documento sin títulos → una sección, `heading: null`, `sections_found: 0`.
- `schema`: una regla regex captura su campo; una regla que no coincide deja el campo
  `null` y lo lista en `unmatched`.
- `schema`: regla `kind: "label"` captura el resto de la línea.
- `schema`: un patrón catastrófico expira por timeout, el campo queda `null` y el error
  aparece en `summary.rule_errors` (no cuelga el test).

`test_app.py`
- `POST /convert` con un PDF devuelve 200, `application/json` y `Content-Disposition`.
- Dos PDF devuelven un ZIP con dos entradas `.json`.
- Un PDF válido y uno corrupto devuelven ZIP con un `.json` y un `_errors.json`.
- Fichero que excede `MAX_FILE_MB` devuelve 413.
- Un fichero que no es PDF devuelve 422.
- Regex inválido devuelve 422 nombrando la regla.
- **Tras cada petición, incluidas las que fallan, no queda ningún directorio
  `pdf2json-*` en el directorio temporal.** Esta es la prueba de la promesa de no
  almacenamiento y es obligatoria.

## 13. Despliegue

`Dockerfile` sobre `python:3.11-slim`, usuario no root, `uvicorn` con 2 workers.
`docker-compose.yml` con `tmpfs: /tmp`, la variable de entorno de límites y un healthcheck
contra `/health`. Sin volúmenes persistentes: el contenedor no tiene nada que conservar.

El README documenta: ejecución local (`uvicorn pdf2json.app:app --reload`), despliegue con
compose, la tabla de variables de entorno, la sección de privacidad del punto 9 y la
licencia AGPL-3.0 con su implicación (quien modifique y ofrezca el servicio debe publicar
su código).
