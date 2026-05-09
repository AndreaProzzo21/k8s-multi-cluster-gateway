# --- STAGE 1: Base ---
FROM python:3.11-slim AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PYTHONPATH=/app

# Installiamo curl (necessario per scaricare Helm) e le dipendenze minime
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Scarica e installa Helm 3
RUN curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
    && chmod 700 get_helm.sh \
    && ./get_helm.sh \
    && rm get_helm.sh

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- STAGE 2: Test ---
FROM base AS test
RUN pip install --no-cache-dir pytest pytest-asyncio httpx pytest-cov

# Copiamo tutto il contenuto di backend (che include app/ e tests/)
COPY . .

# Comando di default per i test
CMD ["pytest"]

# --- STAGE 3: Production ---
FROM base AS prod
# Copiamo solo la cartella dell'applicazione per mantenere l'immagine pulita
COPY ./app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]