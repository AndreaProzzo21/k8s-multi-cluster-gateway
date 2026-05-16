# --- STAGE 1: Base ---
FROM python:3.11-slim AS base
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
    && chmod 700 get_helm.sh \
    && ./get_helm.sh \
    && rm get_helm.sh

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- STAGE 2: Test ---
FROM base AS test
RUN pip install --no-cache-dir pytest pytest-asyncio httpx pytest-cov
COPY . .
CMD ["pytest"]

# --- STAGE 3: Production ---
FROM base AS prod
# Importante: creiamo la cartella data esplicitamente
RUN mkdir -p /app/data

# Copiamo l'applicazione
COPY ./app ./app

# Se hai file di supporto o config nella root del backend (es. alembic.ini se lo usi)
# ricordati di copiarli qui. Se hai solo la cartella app, va bene così.

EXPOSE 8000

# Usiamo il comando uvicorn classico. 
# NOTA: --workers 1 è perfetto per SQLite per evitare lock del database.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]