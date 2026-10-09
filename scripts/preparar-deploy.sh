#!/usr/bin/env bash
# Deja en deploy/ lo mínimo que hay que subir al VPS, y nada más.
#
# Se puede ejecutar siempre que cambie la app: regenera todo salvo deploy/.env,
# que es donde el servidor guarda sus ajustes y por eso nunca se pisa.
#
# Uso: ./scripts/preparar-deploy.sh
set -euo pipefail

cd "$(dirname "$0")/.."
destino="deploy"
version="$(git rev-parse --short HEAD 2>/dev/null || echo desconocida)"
sucio=""
if ! git diff --quiet HEAD 2>/dev/null; then
  sucio=" (con cambios sin commitear)"
fi

mkdir -p "$destino"
# Limpia lo generado la vez anterior; conserva el .env del servidor.
find "$destino" -mindepth 1 -maxdepth 1 ! -name .env -exec rm -rf {} +

cp -R pdf2json "$destino/pdf2json"
find "$destino/pdf2json" -name __pycache__ -type d -exec rm -rf {} +
find "$destino/pdf2json" -name '*.pyc' -delete
cp pyproject.toml Dockerfile .dockerignore docker-compose.yml LICENSE "$destino/"

# requirements.txt para los paneles que instalan así. Se genera desde
# pyproject.toml para que no haya dos listas de dependencias que diverjan.
# Se extrae con sed y no con tomllib porque el python3 del sistema puede ser
# anterior al 3.11, y este guion tiene que funcionar en cualquier máquina.
{
  echo "# Generado por scripts/preparar-deploy.sh desde pyproject.toml. No editar."
  sed -n '/^dependencies = \[/,/^]/p' pyproject.toml |
    sed -n 's/^ *"\(.*\)",*$/\1/p'
} > "$destino/requirements.txt"

if [ "$(grep -cv '^#' "$destino/requirements.txt")" -lt 1 ]; then
  echo "error: no se extrajo ninguna dependencia de pyproject.toml" >&2
  exit 1
fi

cat > "$destino/.env.example" <<'EOF'
# Copia este fichero a .env y ajusta lo que quieras. Todo es opcional:
# sin .env, la app usa estos mismos valores.
# PUERTO solo lo usa Docker: es el puerto del servidor que se publica.
PUERTO=8000
MAX_FILE_MB=25
MAX_FILES=10
MAX_PAGES=500
RATE_LIMIT=10/minute
CONVERT_TIMEOUT_S=30
MAX_REQUEST_MB=60
MAX_CONCURRENTES=4

# Identificación del titular en las páginas legales. Solo hace falta cambiarlas
# si alojas tu propia instancia.
LEGAL_TITULAR=TecXarT
LEGAL_EMAIL=tecxart@gmail.com
EOF

cat > "$destino/VERSION" <<EOF
pdf2json
commit: ${version}${sucio}
preparado: $(date -u '+%Y-%m-%d %H:%M UTC')
EOF

cat > "$destino/DESPLIEGUE.md" <<'EOF'
# Desplegar pdf2json en el VPS

Todo lo necesario está en esta carpeta. No hace falta clonar el repositorio ni
instalar Python en el servidor: solo Docker.

## 1. Subir la carpeta

```bash
rsync -av --delete deploy/ usuario@tu-vps:/opt/pdf2json/
```

`--delete` borra en el servidor lo que ya no esté en la carpeta de origen. El
`.env` del servidor sobrevive igualmente, porque `rsync` no lo borra si tú lo
creaste allí y aquí no existe: para estar seguro, usa
`--exclude .env` junto con `--delete`.

## 2. Ajustar límites (opcional)

En el servidor:

```bash
cd /opt/pdf2json
cp .env.example .env
nano .env
```

Sin `.env` la app arranca con los valores por defecto.

## 3. Arrancar

```bash
cd /opt/pdf2json
docker compose up -d --build
curl -s http://localhost:8000/health
```

Debe responder `{"status":"ok"}`.

## 4. Actualizar

Vuelve a subir la carpeta y reconstruye:

```bash
rsync -av --delete --exclude .env deploy/ usuario@tu-vps:/opt/pdf2json/
ssh usuario@tu-vps 'cd /opt/pdf2json && docker compose up -d --build'
```

`VERSION` dice de qué commit salió lo que hay en la carpeta.

## 5. Ponerlo en internet

El contenedor escucha en el puerto 8000 del propio servidor. Para exponerlo con
HTTPS, pon delante un proxy inverso (Caddy, nginx o Traefik). Con Caddy basta un
Caddyfile de dos líneas:

```
tu-dominio.com {
    reverse_proxy localhost:8000
}
```

Sube también el límite de tamaño de subida del proxy, o rechazará los PDF
grandes antes de que lleguen a la app. En nginx es `client_max_body_size 30m;`.
Caddy no limita por defecto.

## Qué hace con los ficheros

Los PDF se escriben en un directorio temporal y se borran al terminar la
conversión, también si falla. `docker-compose.yml` monta `/tmp` como `tmpfs`,
así que viven en RAM y no tocan el disco del servidor. No hay base de datos ni
volúmenes persistentes: si borras el contenedor no queda nada.

## Comprobaciones útiles

```bash
docker compose logs -f                 # registro
docker compose ps                      # estado y healthcheck
docker compose exec pdf2json ls /tmp   # no debe haber directorios pdf2json-*
```
EOF

echo "deploy/ preparado desde el commit ${version}${sucio}:"
find "$destino" -type f | sort | sed 's/^/  /'
