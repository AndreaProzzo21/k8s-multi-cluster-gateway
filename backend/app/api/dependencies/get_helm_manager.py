"""
get_helm_manager.py
===================

Dependency FastAPI che autentica ogni request e costruisce un ``HelmManager``
configurato per il cluster specificato nel JWT.

Flusso
------
1. Delega autenticazione JWT + recupero credenziali a ``get_cluster_credentials``.
2. Genera un kubeconfig temporaneo via ``temp_kubeconfig`` (context manager).
3. Istanzia e restituisce ``HelmManager`` al route handler tramite ``yield``.
4. Al termine della request (o in caso di eccezione), il ``finally`` del
   context manager rimuove il kubeconfig temporaneo dal disco.

Pattern yield
-------------
L'uso di ``yield`` invece di ``return`` è necessario per garantire il cleanup
del file kubeconfig dopo che la response è stata inviata al client. FastAPI
supporta nativamente le dependency con ``yield`` come context manager impliciti:
il codice dopo il ``yield`` viene eseguito in un blocco ``finally`` interno.

Separazione da get_core_manager
---------------------------------
Le due dependency sono intenzionalmente separate perché:
- Hanno lifecycle diversi: CoreManager non ha cleanup, HelmManager sì.
- Le route K8s e Helm possono evolvere indipendentemente.
- Un route handler può dipendere da entrambe senza che FastAPI esegua
  ``get_cluster_credentials`` due volte (è cachata per scope di request).
"""

from typing import AsyncGenerator

from fastapi import Depends, HTTPException, status

from app.api.dependencies.get_cluster_credentials import (
    ClusterCredentials,
    get_cluster_credentials,
)
from app.core.helm_manager import HelmManager
from app.infrastructure.helm_kubeconfig import temp_kubeconfig


async def get_helm_manager(
    creds: ClusterCredentials = Depends(get_cluster_credentials),
) -> AsyncGenerator[HelmManager, None]:
    """
    Dependency FastAPI: genera un kubeconfig temporaneo e restituisce
    un HelmManager pronto all'uso. Il kubeconfig viene rimosso al termine
    della request.

    Parameters
    ----------
    creds : ClusterCredentials
        Credenziali iniettate da ``get_cluster_credentials``.

    Yields
    ------
    HelmManager
        Configurato con il kubeconfig del cluster specificato nel JWT.

    Raises
    ------
    HTTPException 503
        Se la creazione del kubeconfig temporaneo fallisce (es. /tmp piena,
        permessi del filesystem).
    HTTPException 503
        Se il binario ``helm`` non è presente nel container.
    """
    try:
        with temp_kubeconfig(
            cluster_host=creds.cluster_host,
            k8s_token=creds.k8s_token,
            ca_cert=creds.ca_cert,
            cluster_id=creds.cluster_id,
        ) as kubeconfig_path:
            manager = HelmManager(
                kubeconfig_path=kubeconfig_path,
                cluster_id=creds.cluster_id,
            )

            # Verifica che il binario helm sia disponibile prima di procedere.
            # Fallisce veloce qui invece di scoprirlo al primo comando helm.
            if not manager.helm_available:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "Il binario 'helm' non è disponibile nel container. "
                        "Aggiungere 'helm' all'immagine Docker del gateway."
                    ),
                )

            yield manager

            # Il context manager temp_kubeconfig rimuove il file qui,
            # dopo che il route handler ha terminato e la response è inviata.

    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Impossibile creare il kubeconfig temporaneo: {exc}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Errore interno durante l'inizializzazione di Helm: {exc}",
        )