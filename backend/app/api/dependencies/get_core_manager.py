"""
get_core_manager.py
===================

Dependency FastAPI che autentica ogni request e costruisce il CoreManager
configurato per il cluster specificato nel JWT.

Flusso
------
1. Estrae e valida il JWT dall'header ``Authorization: Bearer``.
2. Recupera **dal database** sia il CA Certificate che il ``k8s_token``
   del profilo — nessuna credenziale K8s viaggia nel JWT.
3. Istanzia i client K8s tramite la factory in un thread separato
   (``run_in_executor``) per non bloccare l'event loop asyncio.
4. Restituisce il ``CoreManager`` al route handler.

Perché il k8s_token viene recuperato dal DB
-------------------------------------------
Il JWT è firmato ma non cifrato: il payload è leggibile in base64 da
chiunque abbia il token. Nella versione precedente il k8s_token veniva
incluso nel payload, esponendolo a chiunque riuscisse a leggere il JWT
(es. XSS su localStorage, accesso fisico al browser).

Ora il JWT contiene solo ``cluster_id`` e ``profile`` — dati non sensibili
usati come chiavi per recuperare le credenziali reali dal database server-side.
"""

import asyncio
import urllib3

from functools import partial
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.api.auth.auth_handler import decode_access_token
from app.infrastructure.k8s_factory import K8sClientFactory
from app.core.core_manager import CoreManager
from app.infrastructure.database import SessionLocal, ClusterModel, ProfileModel


security = HTTPBearer()


async def get_current_core_manager(
    res: HTTPAuthorizationCredentials = Depends(security),
) -> CoreManager:
    """
    Dependency FastAPI: autentica la request e restituisce un CoreManager pronto.

    Args:
        res: Credenziali HTTP estratte automaticamente da FastAPI tramite HTTPBearer.

    Returns:
        CoreManager configurato per il cluster e il profilo indicati nel JWT.

    Raises:
        HTTPException 401: JWT assente, malformato, scaduto o payload incompleto.
        HTTPException 404: Cluster o profilo non trovati nel database.
        HTTPException 503: Impossibile inizializzare il client K8s.
    """

    # ------------------------------------------------------------------
    # Step 1 — Decodifica e validazione del JWT
    # ------------------------------------------------------------------
    token = res.credentials

    try:
        payload = decode_access_token(token)
    except HTTPException:
        raise  # decode_access_token solleva già HTTPException corrette
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token non valido o scaduto: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    cluster_id: str | None = payload.get("cluster_id")
    cluster_host: str | None = payload.get("cluster_host")
    profile: str | None = payload.get("profile")

    # Il JWT non contiene più k8s_token: verifichiamo solo i campi presenti
    if not all([cluster_id, cluster_host, profile]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Payload JWT incompleto: cluster_id, cluster_host o profile mancanti.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ------------------------------------------------------------------
    # Step 2 — Recupero credenziali dal database
    # ------------------------------------------------------------------
    # CA Certificate + k8s_token vengono recuperati server-side.
    # Il k8s_token non viaggia nel JWT: rimane nel DB e viene letto qui
    # ad ogni request, esattamente come il CA cert.
    ca_cert: str | None = None
    k8s_token: str | None = None

    db = SessionLocal()
    try:
        # Cluster: necessario per CA cert e per validare l'esistenza
        cluster = db.query(ClusterModel).filter(
            ClusterModel.id == cluster_id
        ).first()

        if cluster is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Cluster '{cluster_id}' non trovato nel registro.",
            )

        ca_cert = cluster.ca_cert

        # Profilo: necessario per il k8s_token
        # Usiamo .upper() sul cluster_id per coerenza con ClusterRegistry
        # (vedi registry.py: ProfileModel.cluster_id == cluster_id.upper())
        profile_record = db.query(ProfileModel).filter(
            ProfileModel.cluster_id == cluster_id.upper(),
            ProfileModel.name == profile,
        ).first()

        if profile_record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profilo '{profile}' non trovato per il cluster '{cluster_id}'.",
            )

        k8s_token = profile_record.k8s_token

    finally:
        # Il finally garantisce chiusura della sessione DB anche in caso
        # di HTTPException sollevata nel blocco try.
        db.close()

    # ------------------------------------------------------------------
    # Step 3 — Inizializzazione del client K8s (in executor)
    # ------------------------------------------------------------------
    # K8sClientFactory.get_apis è sincrona: legge/scrive file su disco e
    # alloca il pool urllib3. Eseguirla nell'event loop asyncio bloccherebbe
    # il gateway per tutta la sua durata.
    try:
        loop = asyncio.get_running_loop()
        k8s_apis = await loop.run_in_executor(
            None,
            partial(
                K8sClientFactory.get_apis,
                cluster_host=cluster_host,
                k8s_token=k8s_token,
                ca_cert=ca_cert,
                cluster_id=cluster_id,
            ),
        )
    except ValueError as exc:
        # ValueError dalla factory = CA cert mancante o non valido
        print(f"[get_core_manager] CA cert non valido per '{cluster_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    except (urllib3.exceptions.MaxRetryError,
            urllib3.exceptions.ConnectTimeoutError,
            urllib3.exceptions.NewConnectionError,
            OSError) as exc:
        print(f"[get_core_manager] Errore rete/filesystem per '{cluster_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Impossibile connettersi al cluster '{cluster_id}'.",
        )
    except Exception as exc:
        print(f"[get_core_manager] Errore imprevisto factory per '{cluster_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Errore interno durante l'inizializzazione del client K8s.",
        )

    # ------------------------------------------------------------------
    # Step 4 — Restituzione del CoreManager
    # ------------------------------------------------------------------
    return CoreManager(k8s_apis)