# pdf2json

Convierte PDF a JSON desde el navegador. Sube uno o varios PDF, elige el modo y descarga
el resultado. **El servidor no conserva ningún fichero.**

## Modos

| Modo | Salida |
| --- | --- |
| `raw` | Texto por página: `{metadata, pages:[{number, text, ocr_required}]}` |
| `layout` | Secciones anidadas con títulos y tablas. Reconoce `CAPÍTULO`, `Artículo N`, `Disposición...` |
| `schema` | Solo los campos que indiques, por regex o por etiqueta. Plantillas para Ley/BOE, factura y CV |

Toda salida lleva `schema_version`, `mode`, `metadata` y `summary.pages_without_text`.

## Privacidad

Cada petición trabaja en su propio directorio temporal (`/tmp/pdf2json-*`), que se borra al
terminar la conversión, también si falla, si expira el timeout o si cortas la conexión. Al
arrancar, el servicio barre los temporales huérfanos que hubiera dejado un proceso muerto.
`docker-compose.yml` monta `/tmp` como `tmpfs`, así que los PDF viven en RAM y no tocan el
disco físico.

No hay base de datos ni caché. Los registros del servidor no guardan el nombre del fichero
subido, su contenido, los campos extraídos ni los patrones que escribes.

## Sin OCR

Si una página no tiene capa de texto (PDF escaneado), sale con `text: ""` y
`ocr_required: true`, y su número aparece en `summary.pages_without_text`. Esta
herramienta no hace OCR.

## Ejecutar

Local:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn pdf2json.app:app --reload
```

Docker:

```bash
docker compose up --build
```

En `http://localhost:8000`.

Tests:

```bash
.venv/bin/pytest
```

## Configuración

| Variable | Defecto | Qué hace |
| --- | --- | --- |
| `MAX_FILE_MB` | 25 | Tamaño máximo por fichero |
| `MAX_FILES` | 10 | Ficheros por petición |
| `MAX_PAGES` | 500 | Páginas por documento |
| `RATE_LIMIT` | `10/minute` | Peticiones por IP |
| `CONVERT_TIMEOUT_S` | 30 | Segundos por fichero antes de cancelar |

El contador del rate limit vive en la memoria de cada worker de uvicorn, así que el límite
real se multiplica por el número de workers. Con 1-2 workers es suficiente; para precisión,
`slowapi` admite Redis.

## Límites conocidos

- La detección de secciones del modo `layout` es heurística: tamaño de fuente más patrones
  legales en español. Un PDF con maquetación rara devuelve una jerarquía plana, nunca un
  error.
- Las reglas del modo `schema` se ejecutan con un timeout de 1 s cada una. La regla que lo
  supere deja su campo a `null` y aparece en `summary.rule_errors`.
- Sin OCR.

## Licencia

AGPL-3.0-or-later, impuesta por PyMuPDF. Si modificas este código y ofreces el servicio por
red, debes publicar tu versión.
