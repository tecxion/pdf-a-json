FROM python:3.11-slim

RUN useradd --create-home --uid 10001 app
WORKDIR /srv

COPY pyproject.toml ./
COPY pdf2json ./pdf2json
RUN pip install --no-cache-dir .

USER app
EXPOSE 8085

# 2 workers de uvicorn; cada uno abre su propio pool de 2 procesos de conversión.
# El puerto sale de PORT para que un panel pueda fijarlo sin reconstruir la imagen.
# El exec deja a uvicorn como PID 1, así recibe las señales de parada de Docker.
CMD ["sh", "-c", "exec uvicorn pdf2json.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
