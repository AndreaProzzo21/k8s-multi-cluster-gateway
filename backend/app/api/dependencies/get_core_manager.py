import asyncio
import urllib3

from functools import partial
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.api.auth.auth_handler import decode_access_token
from app.infrastructure.k8s_factory import K8sClientFactory
from app.core.core_manager import CoreManager
from app.infrastructure.database import SessionLocal, ClusterModel


security = HTTPBearer()


async def get_current_core_manager(
    res: HTTPAuthorizationCredentials = Depends(security),
) -> CoreManager:
    """
    Dependency FastAPI che autentica la richiesta e restituisce un CoreManager
    configurato e pronto all'uso per il cluster specificato nel JWT.

    Viene eseguita prima di ogni endpoint protetto e si occupa di:
      1. Estrarre e validare il JWT dall'header Authorization.
      2. Recuperare il CA Certificate del cluster dal database.
      3. Istanziare i client K8s tramite la factory (in un thread separato).
      4. Restituire il CoreManager al route handler.

    Il punto 3 viene eseguito con `run_in_executor` perché K8sClientFactory.get_apis
    è una funzione sincrona/bloccante (scrive un file temporaneo, alloca il pool
    urllib3). Spostarla fuori dall'event loop asyncio garantisce che il gateway
    rimanga responsivo anche durante la fase di setup del client, specialmente
    quando il cluster è lento o irraggiungibile.

    Args:
        res: Credenziali HTTP estratte automaticamente da FastAPI tramite HTTPBearer.

    Returns:
        CoreManager configurato per il cluster e il profilo indicati nel JWT.

    Raises:
        HTTPException 401: JWT assente, malformato o scaduto.
        HTTPException 404: Cluster non trovato nel database.
        HTTPException 503: Impossibile inizializzare il client K8s (errore factory).
    """

    # ------------------------------------------------------------------
    # Step 1: Decodifica e validazione del JWT
    # ------------------------------------------------------------------
    # decode_access_token solleva un'eccezione propria in caso di token
    # invalido o scaduto; la convertiamo in un 401 standard per FastAPI.
    token = res.credentials

    try:
        payload = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token non valido o scaduto: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    cluster_id: str | None = payload.get("cluster_id")
    cluster_host: str | None = payload.get("cluster_host")
    k8s_token: str | None = payload.get("k8s_token")

    # Verifica che i campi obbligatori siano presenti nel payload.
    # Un JWT valido ma con payload incompleto indica una corruzione o
    # una versione precedente del token: rifiutiamo con 401.
    if not all([cluster_id, cluster_host, k8s_token]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Payload JWT incompleto: campi obbligatori mancanti.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ------------------------------------------------------------------
    # Step 2: Recupero del CA Certificate dal database
    # ------------------------------------------------------------------
    # Il CA Certificate non viene incluso nel JWT (troppo grande e non
    # necessario lato client): viene recuperato dal DB ad ogni richiesta
    # usando il cluster_id come chiave. Questo garantisce che una rotazione
    # del certificato sia immediatamente effettiva senza re-emettere token.
    ca_cert: str | None = None

    db = SessionLocal()
    try:
        cluster = db.query(ClusterModel).filter(ClusterModel.id == cluster_id).first()

        if cluster is None:
            # Il cluster_id nel token non corrisponde a nessun cluster registrato.
            # Può succedere se il cluster viene eliminato dal DB dopo l'emissione del JWT.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Cluster '{cluster_id}' non trovato nel registro.",
            )

        ca_cert = cluster.ca_cert  # Può essere None se il cluster non ha SSL configurato

    finally:
        # Il blocco finally garantisce che la sessione DB venga sempre chiusa,
        # anche se viene sollevata l'HTTPException sopra.
        db.close()

    # ------------------------------------------------------------------
    # Step 3: Inizializzazione del client K8s (in executor)
    # ------------------------------------------------------------------
    # K8sClientFactory.get_apis è sincrona e potenzialmente bloccante:
    # - Scrive un file temporaneo per il CA cert (I/O su disco).
    # - Alloca il pool di connessioni urllib3.
    #
    # Eseguirla direttamente in una coroutine async bloccherebbe l'event loop
    # di asyncio per tutta la sua durata, impedendo ad altre richieste di
    # essere processate nel frattempo.
    #
    # Con run_in_executor la factory gira in un thread del ThreadPoolExecutor
    # di default, liberando l'event loop immediatamente.
    try:
        loop = asyncio.get_running_loop()
        k8s_apis = await loop.run_in_executor(
            None,  # None = ThreadPoolExecutor di default gestito da asyncio
            partial(
                K8sClientFactory.get_apis,
                cluster_host=cluster_host,
                k8s_token=k8s_token,
                ca_cert=ca_cert,
                cluster_id=cluster_id,
            ),
        )
    except (urllib3.exceptions.MaxRetryError,
            urllib3.exceptions.ConnectTimeoutError,
            urllib3.exceptions.NewConnectionError,
            OSError) as exc:
        # Errori di rete o filesystem durante la creazione del client.
        # Non esponiamo dettagli interni al client: logghiamo e restituiamo 503.
        print(f"[get_core_manager] Errore inizializzazione client K8s per '{cluster_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Impossibile connettersi al cluster '{cluster_id}'. Verificare che sia raggiungibile.",
        )
    except Exception as exc:
        # Fallback generico per errori imprevisti nella factory.
        print(f"[get_core_manager] Errore imprevisto factory per '{cluster_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Errore interno durante l'inizializzazione del client K8s.",
        )

    # ------------------------------------------------------------------
    # Step 4: Restituzione del CoreManager al route handler
    # ------------------------------------------------------------------
    return CoreManager(k8s_apis)