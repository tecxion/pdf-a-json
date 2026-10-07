FROM python:3.11-slim

RUN useradd --create-home --uid 10001 app
WORKDIR /srv

COPY pyproject.toml ./
COPY pdf2json ./pdf2json
RUN pip install --no-cache-dir .

USER app
EXPOSE 8000

# 2 workers de uvicorn; cada uno abre su propio pool de 2 procesos de conversión.
CMD ["uvicorn", "pdf2json.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
