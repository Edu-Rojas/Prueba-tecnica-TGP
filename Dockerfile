FROM python:3.12-slim

# Binario de uv copiado desde su imagen oficial, con versión fija para builds reproducibles.
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Primero solo los archivos de dependencias: si el código cambia pero el lock no,
# Docker reutiliza esta capa y no reinstala todo.
COPY pyproject.toml uv.lock .python-version ./
# --locked (y no --frozen): si uv.lock no coincide con pyproject.toml el build falla,
# en vez de construir una imagen a la que le faltan dependencias.
RUN uv sync --locked --no-dev

COPY . .

# No correr como root dentro del contenedor.
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

# La api aplica las migraciones antes de arrancar (el worker sobreescribe este comando y no migra,
# así nunca hay dos procesos migrando a la vez).
# Forma "sh -c" para poder expandir ${PORT}: Render inyecta el puerto en esa variable.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
