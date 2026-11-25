# syntax=docker/dockerfile:1
# Una sola imagen para API, worker y migraciones (se diferencian por el comando de Compose).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app
RUN useradd --create-home --uid 10001 app

# Dependencias primero: se reutiliza la capa mientras no cambie pyproject.toml
COPY pyproject.toml ./
COPY app/__init__.py ./app/__init__.py
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY fixtures ./fixtures
COPY app ./app

USER app
EXPOSE 8000

# Por defecto corre el worker; Compose sobrescribe el comando para la API y las migraciones.
CMD ["python", "-m", "app.worker"]
