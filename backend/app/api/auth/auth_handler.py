import jwt
import datetime
import os
import secrets
from fastapi import HTTPException, status
from app.core.registry import ClusterRegistry

JWT_SECRET = os.getenv("JWT_SECRET_KEY")
ALGORITHM = os.getenv("JWT_SECRET_ALGORITHM", "HS256")
TOKEN_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "1"))

# Fail fast all'avvio se il secret non è configurato.
# Mai usare un default: se manca la variabile d'ambiente il processo non parte.
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET_KEY non configurato. "
        "Impostare la variabile d'ambiente prima di avviare il gateway."
    )

if JWT_SECRET == "change-me-in-production":
    raise RuntimeError(
        "JWT_SECRET_KEY ha ancora il valore di default. "
        "Generare un secret sicuro: python -c \"import secrets; print(secrets.token_hex(32))\""
    )


def create_access_token(cluster_id: str, profile: str, password: str) -> str:
    """
    Verifica le credenziali e restituisce un JWT che identifica la sessione.

    Il JWT NON contiene il k8s_token — contiene solo cluster_id e profile,
    che vengono usati server-side per recuperare il token K8s dal DB ad ogni
    request. Questo limita l'impatto di una compromissione del JWT: l'attaccante
    ottiene solo un identificatore, non le credenziali K8s reali.
    """
    cluster_data = ClusterRegistry.get_cluster_data(cluster_id, profile)

    if not cluster_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{profile}' not found in cluster '{cluster_id}'"
        )

    # Confronto costante nel tempo per prevenire timing attacks sulla password.
    if not secrets.compare_digest(password, cluster_data["gateway_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    payload = {
        "cluster_id": cluster_id,
        "cluster_host": cluster_data["host"],  # host è pubblico, ok nel JWT
        "profile": profile,
        # jti = JWT ID: identificatore univoco per questa sessione.
        # Utile in futuro per una blocklist di revoca.
        "jti": secrets.token_hex(16),
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_EXPIRE_HOURS),
    }

    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Valida il JWT e restituisce il payload."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token scaduto")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token non valido")