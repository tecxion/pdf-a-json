# pdf2json

Convierte PDF a JSON desde el navegador. Sube uno o varios PDF, elige el modo y descarga
el resultado. **El servidor no conserva ningún fichero.**

## Modos

| Modo | Salida |
| --- | --- |
| `auto` | Por defecto. Ejecuta `layout` y lo devuelve si encontró al menos 3 títulos; si no, devuelve `raw`. La decisión y su motivo van en `summary.auto` |
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

## PDF escaneados

Si una página no tiene capa de texto, sale con `text: ""` y `ocr_required: true`,
y su número aparece en `summary.pages_without_text`. Si **ninguna** página tiene
texto, la conversión falla con un mensaje claro en vez de devolver un JSON vacío.

Hay OCR opcional, apagado por defecto porque multiplica el tiempo de conversión:

```bash
docker compose build --build-arg CON_OCR=1
OCR_ENABLED=1 docker compose up -d
```

Solo se aplica a las páginas que no tienen texto. Si se activa sin Tesseract en
la imagen, se avisa una vez en el registro y esas páginas siguen saliendo
vacías.

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

## Usarlo desde un script

El mismo endpoint sirve para la web y para la API. Con `Accept: application/json`
los errores llegan en JSON en vez de en una página HTML.

```bash
curl -sS -X POST https://tu-dominio/convert \
  -H "Accept: application/json" \
  -F "mode=auto" \
  -F "files=@documento.pdf" \
  -o salida.json
```

Modo campos, con reglas repetidas en el mismo orden (nombre, tipo, patrón):

```bash
curl -sS -X POST https://tu-dominio/convert \
  -H "Accept: application/json" -F "mode=schema" -F "files=@ley.pdf" \
  --form-string 'rule_name=numero' --form-string 'rule_kind=regex' \
  --form-string 'rule_pattern=Ley\s+(\d+/\d{4})' \
  -o campos.json
```

Usa `--form-string` para los patrones: `-F` interpreta `@` y `<` al principio del
valor. Con varios ficheros la respuesta es un ZIP. La documentación OpenAPI está
en `/docs`.

## Configuración

| Variable | Defecto | Qué hace |
| --- | --- | --- |
| `MAX_FILE_MB` | 25 | Tamaño máximo por fichero |
| `MAX_FILES` | 10 | Ficheros por petición |
| `MAX_PAGES` | 500 | Páginas por documento |
| `RATE_LIMIT` | `10/minute` | Peticiones por IP |
| `CONVERT_TIMEOUT_S` | 30 | Segundos por fichero antes de cancelar |
| `MAX_REQUEST_MB` | 60 | Tamaño de la petición entera, cortado antes de leer el cuerpo |
| `MAX_CONCURRENTES` | 4 | Conversiones a la vez; por encima se responde 503 |
| `OCR_ENABLED` | `0` | `1` activa OCR en las páginas sin texto (requiere imagen con `CON_OCR=1`) |
| `OCR_IDIOMA` | `spa` | Idioma de Tesseract |

El contador del rate limit vive en la memoria de cada worker de uvicorn, así que el límite
real se multiplica por el número de workers. Con 1-2 workers es suficiente; para precisión,
`slowapi` admite Redis.

## Desplegar en un servidor

```bash
./scripts/preparar-deploy.sh
```

Deja en `deploy/` solo lo que hay que subir: el paquete, `pyproject.toml`, el
`Dockerfile`, el `docker-compose.yml`, la licencia y un `DESPLIEGUE.md` con los
pasos. Esa carpeta está en `.gitignore`: se regenera, no se versiona.

El guion se puede volver a ejecutar cada vez que cambie la app. Regenera todo
menos `deploy/.env`, que es donde el servidor guarda sus ajustes.

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
