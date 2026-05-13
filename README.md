# Kubernetes Multi-Cluster Access Gateway

> A zero-knowledge, multi-tenant Kubernetes management platform. Credentials never leave the server — users get access, not keys.

[![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green?logo=fastapi)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue?logo=docker)](https://docker.com)
[![Helm](https://img.shields.io/badge/Helm-3.x-blue?logo=helm)](https://helm.sh)

---

## What is this platform?

This gateway is a self-hosted web platform that acts as an authenticated proxy between your users and your Kubernetes clusters. Instead of distributing `kubeconfig` files or Service Account tokens, the platform issues short-lived JWTs that contain **no Kubernetes credentials**. Every real credential — SA token, CA certificate — lives exclusively in the server-side database and is injected per-request, invisible to the client.

The result is a team-friendly control plane where access is managed through profiles, revocation is instant, and the blast radius of a stolen JWT is limited to what the gateway exposes — not direct cluster access.

**Two integrated consoles:**

- **K8s Console** — real-time visibility and operations: namespaces, pods, deployments, services, ingresses, RBAC, storage, events.
- **Helm Console** — application lifecycle management: install charts from repositories or ZIP uploads, inspect history, rollback, lint before deploying.

---

## Core Design Principles

**Zero-knowledge client side.** The browser JWT contains only `cluster_id` and `profile`. The Kubernetes SA token and CA certificate are fetched server-side from the database on every authenticated request and discarded after use.

**Stateless architecture.** The gateway holds no session state. Each request is fully self-contained: verify JWT → fetch credentials from DB → build scoped K8s client → forward request → discard client.

**K8s enforces authorization.** The gateway delegates all resource-level access control to Kubernetes RBAC. A restricted Service Account will receive `403` from the cluster; the gateway propagates it to the frontend. No shadow permission system.

**Profile-based multi-tenancy.** Each cluster supports multiple profiles (e.g. `admin`, `dev`, `ci`), each mapping to a different Service Account. A user authenticates against a profile, not against the cluster directly.

**Per-cluster Helm isolation.** Each cluster maintains its own Helm repository configuration and cache, invisible to users of other clusters.

---

## High-Level Architecture

```mermaid
graph TB
    subgraph Browser["Browser"]
        K8sUI["K8s Console<br/>dashboard.html"]
        HelmUI["Helm Console<br/>helm.html"]
    end

    subgraph Gateway["API Gateway — FastAPI"]
        Auth["Auth Layer<br/>POST /auth/login"]
        SharedDep["get_cluster_credentials<br/>JWT decode + DB fetch"]
        CoreMgr["CoreManager<br/>kubernetes SDK"]
        HelmMgr["HelmManager<br/>helm CLI subprocess"]
        K8sFactory["K8sClientFactory<br/>TLS client builder"]
        DB[("SQLite Database<br/>clusters · profiles")]
    end

    subgraph Clusters["Kubernetes Infrastructure"]
        C1["Cluster A"]
        C2["Cluster B"]
        CN["Cluster N"]
    end

    K8sUI -->|Bearer JWT| Auth
    HelmUI -->|Bearer JWT| Auth
    Auth --> SharedDep
    SharedDep -->|ca_cert + k8s_token| DB
    SharedDep --> CoreMgr
    SharedDep --> HelmMgr
    CoreMgr --> K8sFactory
    K8sFactory -->|TLS + Bearer| C1
    K8sFactory -->|TLS + Bearer| C2
    K8sFactory -->|TLS + Bearer| CN
    HelmMgr -->|temp kubeconfig| C1
    HelmMgr -->|temp kubeconfig| C2
```

---

## Authentication Flow

```mermaid
sequenceDiagram
    actor User as User (Browser)
    participant GW as API Gateway
    participant DB as Database
    participant K8s as K8s Cluster

    Note over User,GW: Phase 1 — Login
    User->>GW: POST /auth/login<br/>{cluster_id, profile, password}
    GW->>DB: verify credentials
    DB-->>GW: ok
    GW-->>User: JWT {cluster_id, profile, exp}<br/>⚠ no k8s_token inside

    Note over User,K8s: Phase 2 — Resource Request
    User->>GW: GET /api/v1/namespaces/{ns}/pods<br/>Authorization: Bearer JWT
    GW->>GW: decode & validate JWT
    GW->>DB: fetch ca_cert for cluster_id
    GW->>DB: fetch k8s_token for profile
    DB-->>GW: credentials
    GW->>GW: build K8s client (TLS + Bearer)
    GW->>K8s: forward request
    K8s-->>GW: response (K8s enforces RBAC)
    GW-->>User: JSON response
```

**JWT payload contains:** `cluster_id`, `cluster_host`, `profile`, `jti`, `exp`
**JWT payload never contains:** `k8s_token`, `ca_cert`, `password`

---

## Helm Request Flow

```mermaid
sequenceDiagram
    actor User as User (Browser)
    participant GW as API Gateway
    participant DB as Database
    participant FS as Filesystem /tmp
    participant Helm as helm CLI

    User->>GW: POST /api/v1/helm/namespaces/{ns}/releases/{name}/from-zip<br/>Authorization: Bearer JWT
    GW->>DB: fetch ca_cert + k8s_token
    DB-->>GW: credentials
    GW->>FS: write temp kubeconfig (0600)<br/>/tmp/helm_kube_{cluster_id}_{rand}.yaml
    GW->>FS: extract chart ZIP<br/>/tmp/helm_chart_{cluster_id}_{rand}/
    GW->>Helm: helm upgrade --install<br/>--kubeconfig {temp}<br/>--repository-config /tmp/helm_repos/{cluster_id}/repositories.yaml
    Helm-->>GW: stdout / stderr / rc
    GW->>FS: delete temp kubeconfig
    GW->>FS: delete extracted chart dir
    GW-->>User: {success, stdout, stderr}
```

---

## Admin API

Cluster and profile management is protected by a master key sent in the `X-Admin-Key` HTTP header. This API is intended for platform administrators only and is only exposed through the frontend through a dedicated console.

```
Base path: /api/v1/admin
Header:    X-Admin-Key: <ADMIN_MASTER_KEY>
```

### Clusters

| Method | Path | Description |
|---|---|---|
| `GET` | `/clusters` | List all registered clusters |
| `POST` | `/clusters` | Register a new cluster (`multipart/form-data`: `id`, `name`, `host`, `ca_file`) |
| `PATCH` | `/clusters/{cluster_id}` | Update cluster name, host, or CA certificate |
| `DELETE` | `/clusters/{cluster_id}` | Remove cluster and all associated profiles |

### Profiles

| Method | Path | Description |
|---|---|---|
| `GET` | `/profiles` | List all profiles (token preview only, never full token) |
| `POST` | `/profiles` | Create a profile (`JSON`: `cluster_id`, `name`, `gateway_password`, `k8s_token`) |
| `PATCH` | `/profiles/{profile_id}` | Update password or SA token |
| `DELETE` | `/profiles/{profile_id}` | Remove a profile |

**Register a cluster (example):**

```bash
curl -X POST http://localhost:8000/api/v1/admin/clusters \
  -H "X-Admin-Key: your-admin-key" \
  -F "id=MY-CLUSTER" \
  -F "name=Production K3s" \
  -F "host=https://10.0.0.1:6443" \
  -F "ca_file=@/path/to/ca.crt"
```

**Register a profile (example):**

```bash
curl -X POST http://localhost:8000/api/v1/admin/profiles \
  -H "master-key: your-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "cluster_id": "MY-CLUSTER",
    "name": "dev",
    "gateway_password": "dev-password",
    "k8s_token": "eyJhbGci..."
  }'
```

---

## Project Structure

```
k8s-cloud-gateway/
│
├── docker-compose.yml
├── .env
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py
│       ├── api/
│       │   ├── auth/
|       |   |   ├── auth_routes.py
│       │   │   └── auth_handler.py          # JWT issue & decode
│       │   ├── dependencies/
│       │   │   ├── get_cluster_credentials.py   # shared: JWT + DB → ClusterCredentials
│       │   │   ├── get_core_manager.py           # builds CoreManager
│       │   │   └── get_helm_manager.py           # builds HelmManager + kubeconfig lifecycle
│       │   ├── routes/
│       │   |   ├── k8s_routes.py            # K8s resource endpoints
│       │   |   ├── helm_routes.py           # Helm endpoints
│       │   |   └── admin_routes.py          # Cluster & profile registry
|       |   |__ api_server.py                # API init and settings
|       |
│       ├── core/
│       │   ├── core_manager.py              # K8s operations
│       │   ├── helm_manager.py              # Helm operations
|       |   ├── registry.py
│       │   └── exceptions.py
│       └── infrastructure/
│           ├── k8s_factory.py               # Authenticated K8s client builder
│           ├── helm_kubeconfig.py           # Temp kubeconfig context manager
│           └── database.py                 # SQLAlchemy models + SessionLocal
│
├── frontend/
│   ├── index.html                           # Login
│   ├── dashboard.html                       # K8s Console
│   ├── helm.html                            # Helm Console
│   └── assets/
│       ├── css/style.css
│       └── js/
│           ├── api.js                       # apiCall(), JWT handling, error dispatch
│           ├── ui.js                        # Shared UI helpers
│           └── modules/
│               ├── cluster.js
│               ├── workloads.js
│               ├── network_config.js
│               ├── rbac.js
│               └── helm.js
│
└── data/
    └── gateway.db                           # SQLite (auto-created on first run)
```

---

## Deployment

### Prerequisites

- Docker and Docker Compose
- One or more Kubernetes clusters with Service Accounts and their tokens
- The CA certificate of each cluster (PEM format)

### Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/AndreaProzzo21/k8s-cloud-gateway.git
cd k8s-cloud-gateway

# 2. Configure environment
cp .env.example .env
# Fill in JWT_SECRET_KEY and ADMIN_MASTER_KEY with strong random strings

# 3. Start the stack
docker compose up --build -d

# 4. Register a cluster and a profile (see Admin API section above)

# 5. Open the dashboard
open http://localhost:80
```

### Environment Variables

```dotenv
# JWT signing key — use a long random string, keep it secret
JWT_SECRET_KEY=
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"

# JWT signing algorithm
JWT_SECRET_ALGORITHM=HS256

# Master key for the admin API — protect this carefully
ADMIN_MASTER_KEY=
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"

# SQLite database path
DATABASE_URL=data/gateway.db

# Fernet encryption key for sensitive DB fields (k8s_token, gateway_password, ca_cert)
ENCRYPTION_KEY=
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Docker Compose

```yaml
services:
  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
      target: prod
    container_name: k8s_api_gateway_v1
    ports:
      - "8000:8000"
    env_file:
      - .env
    volumes:
      - ./backend/app:/app/app
      - ./backend/data:/app/data
      - helm_data:/tmp/helm_repos     # Helm repo config — persisted per cluster
    networks:
      - k8s_network
    restart: always

  frontend:
    image: nginx:alpine
    container_name: k8s_frontend_v1
    ports:
      - "80:80"
    volumes:
      - ./frontend:/usr/share/nginx/html:ro
    networks:
      - k8s_network
    restart: always
    depends_on:
      - backend

networks:
  k8s_network:
    driver: bridge

volumes:
  helm_data:
```

> **Why only one volume?** Helm repository configuration (`repositories.yaml` and index cache) lives under `/tmp/helm_repos/{cluster_id}/` — a directory tree managed by `HelmManager` and passed to the `helm` binary via `--repository-config` and `--repository-cache`. This single volume persists all per-cluster repo state across container restarts.

---

## Security Notes

| Topic | Current state | Roadmap |
|---|---|---|
| JWT storage | `localStorage` | Migrate to `HttpOnly` cookies |
| DB credentials at rest | Plaintext in SQLite | AES-256 encryption for `k8s_token` and `password_hash` |
| Authorization | Delegated to K8s RBAC | Optional namespace allowlist per profile |
| Helm kubeconfig | Temp file `0600`, deleted after request | ✅ Done |
| CA certificate | Written to `/tmp` once per cluster, cached | ✅ Done |
| Admin API | Protected by master key header | Consider IP allowlist in production |

---

## Interactive API Documentation

Available at [`http://localhost:8000/docs`](http://localhost:8000/docs) when the gateway is running. All endpoints are documented with request/response schemas and can be tested directly from the browser.

---

## Roadmap

- [ ] `HttpOnly` cookie-based JWT storage to mitigate XSS
- [ ] AES-256 encryption for sensitive database columns
- [ ] Namespace allowlist per profile (enforced server-side before reaching K8s)
- [ ] WebSocket streaming for real-time pod logs
- [ ] Multi-user audit log
- [ ] OCI registry support for Helm chart distribution
- [ ] Helm dependency resolution (`helm dependency update`) before ZIP deploy