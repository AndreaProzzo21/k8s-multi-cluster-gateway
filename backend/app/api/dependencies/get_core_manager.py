"""
get_core_manager.py
===================

Dependency FastAPI che costruisce il CoreManager configurato per il cluster
specificato nel JWT.

Flusso
------
1. Delega autenticazione JWT + recupero credenziali a ``get_cluster_credentials``.
2. Istanzia i client K8s tramite ``K8sClientFactory`` in un thread separato
   (``run_in_executor``) per non bloccare l'event loop asyncio.
3. Restituisce il ``CoreManager`` al route handler.

Separazione delle responsabilità
---------------------------------
Questo modulo non gestisce più JWT né query DB: entrambi sono delegati a
``get_cluster_credentials``. Qui rimane solo la logica di costruzione
del client K8s e del CoreManager.
"""

import asyncio
import urllib3

from functools import partial
from fastapi import Depends, HTTPException, status

from app.api.dependencies.get_cluster_credentials import (
    ClusterCredentials,
    get_cluster_credentials,
)
from app.core.core_manager import CoreManager
from app.infrastructure.k8s_factory import K8sClientFactory


async def get_current_core_manager(
    creds: ClusterCredentials = Depends(get_cluster_credentials),
) -> CoreManager:
    """
    Dependency FastAPI: costruisce e restituisce un CoreManager pronto all'uso.

    Parameters
    ----------
    creds : ClusterCredentials
        Credenziali iniettate da ``get_cluster_credentials``.

    Returns
    -------
    CoreManager
        Configurato per il cluster e il profilo indicati nel JWT.

    Raises
    ------
    HTTPException 503
        CA cert non valido, errore di rete/filesystem, o errore imprevisto
        nella factory K8s.
    """

    # ------------------------------------------------------------------
    # Inizializzazione del client K8s (in executor)
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
                cluster_host=creds.cluster_host,
                k8s_token=creds.k8s_token,
                ca_cert=creds.ca_cert,
                cluster_id=creds.cluster_id,
            ),
        )
    except ValueError as exc:
        # ValueError dalla factory = CA cert mancante o non valido
        print(f"[get_core_manager] CA cert non valido per '{creds.cluster_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    except (
        urllib3.exceptions.MaxRetryError,
        urllib3.exceptions.ConnectTimeoutError,
        urllib3.exceptions.NewConnectionError,
        OSError,
    ) as exc:
        print(f"[get_core_manager] Errore rete/filesystem per '{creds.cluster_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Impossibile connettersi al cluster '{creds.cluster_id}'.",
        )
    except Exception as exc:
        print(f"[get_core_manager] Errore imprevisto factory per '{creds.cluster_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Errore interno durante l'inizializzazione del client K8s.",
        )

    return CoreManager(k8s_apis)