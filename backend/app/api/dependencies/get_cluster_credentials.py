"""
get_cluster_credentials.py
==========================

Funzione di supporto condivisa tra ``get_core_manager`` e ``get_helm_manager``.

Responsabilità
--------------
- Decodificare e validare il JWT dall'header ``Authorization: Bearer``.
- Recuperare dal database le credenziali reali del cluster (CA cert, k8s_token).
- Restituire un dataclass ``ClusterCredentials`` tipizzato e immutabile.

Perché un modulo separato
--------------------------
La logica di autenticazione JWT + query DB era duplicata nelle due dependency.
Centralizzarla qui garantisce:

1. Un solo posto dove aggiornare la logica di validazione del token.
2. Nessuna divergenza silenziosa tra i due flussi (K8s e Helm).
3. Testabilità: ``get_cluster_credentials`` può essere mockato in isolamento
   senza toccare ``get_core_manager`` o ``get_helm_manager``.
"""

from dataclasses import dataclass

import urllib3
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.api.auth.auth_handler import decode_access_token
from app.infrastructure.database import ClusterModel, ProfileModel, SessionLocal


security = HTTPBearer()


@dataclass(frozen=True)
class ClusterCredentials:
    """
    Credenziali complete per un cluster K8s, recuperate server-side.

    Immutabile (frozen=True) per prevenire modifiche accidentali nel codice
    che la riceve. Tutti i campi sono stringhe non-None: la validazione
    avviene in ``get_cluster_credentials`` prima della restituzione.

    Attributes
    ----------
    cluster_id : str
        Identificativo del cluster (es. "K3S-PROD").
    cluster_host : str
        URL dell'API Server K8s (es. "https://10.0.0.1:6443").
    profile : str
        Nome del profilo/service-account usato per questa sessione.
    k8s_token : str
        Bearer token del Service Account. Mai loggato.
    ca_cert : str
        Contenuto PEM del CA Certificate del cluster.
    """
    cluster_id: str
    cluster_host: str
    profile: str
    k8s_token: str
    ca_cert: str


async def get_cluster_credentials(
    res: HTTPAuthorizationCredentials = Depends(security),
) -> ClusterCredentials:
    """
    Dependency FastAPI condivisa: decodifica JWT e recupera credenziali dal DB.

    Utilizzata come sotto-dipendenza da ``get_current_core_manager`` e
    ``get_helm_manager``. FastAPI la eseguirà una sola volta per request
    grazie al meccanismo di caching delle dependency nello stesso scope.

    Parameters
    ----------
    res : HTTPAuthorizationCredentials
        Credenziali Bearer estratte automaticamente dall'header da FastAPI.

    Returns
    -------
    ClusterCredentials
        Dataclass immutabile con tutte le credenziali necessarie per
        costruire un client K8s o un kubeconfig Helm.

    Raises
    ------
    HTTPException 401
        JWT assente, malformato, scaduto o payload incompleto.
    HTTPException 404
        Cluster o profilo non trovati nel database.
    """

    # ------------------------------------------------------------------
    # Step 1 — Decodifica e validazione del JWT
    # ------------------------------------------------------------------
    token = res.credentials

    try:
        payload = decode_access_token(token)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token non valido o scaduto: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    cluster_id: str | None = payload.get("cluster_id")
    cluster_host: str | None = payload.get("cluster_host")
    profile: str | None = payload.get("profile")

    if not all([cluster_id, cluster_host, profile]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Payload JWT incompleto: cluster_id, cluster_host o profile mancanti.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ------------------------------------------------------------------
    # Step 2 — Recupero credenziali dal database
    # ------------------------------------------------------------------
    db = SessionLocal()
    try:
        cluster = db.query(ClusterModel).filter(
            ClusterModel.id == cluster_id
        ).first()

        if cluster is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Cluster '{cluster_id}' non trovato nel registro.",
            )

        ca_cert: str | None = cluster.ca_cert

        # Usiamo .upper() sul cluster_id per coerenza con ClusterRegistry
        profile_record = db.query(ProfileModel).filter(
            ProfileModel.cluster_id == cluster_id.upper(),
            ProfileModel.name == profile,
        ).first()

        if profile_record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profilo '{profile}' non trovato per il cluster '{cluster_id}'.",
            )

        k8s_token: str | None = profile_record.k8s_token

    finally:
        # Garantisce chiusura della sessione anche in caso di HTTPException.
        db.close()

    # ------------------------------------------------------------------
    # Step 3 — Validazione finale e restituzione
    # ------------------------------------------------------------------
    # Questi controlli coprono il caso in cui i campi esistano nel DB
    # ma siano None o stringa vuota (dati corrotti / migrazione incompleta).
    if not ca_cert:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"CA Certificate mancante per il cluster '{cluster_id}'. "
                   "Aggiornare il cluster tramite l'API di registrazione.",
        )

    if not k8s_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"k8s_token mancante per il profilo '{profile}' "
                   f"del cluster '{cluster_id}'.",
        )

    return ClusterCredentials(
        cluster_id=cluster_id,
        cluster_host=cluster_host,
        profile=profile,
        k8s_token=k8s_token,
        ca_cert=ca_cert,
    )