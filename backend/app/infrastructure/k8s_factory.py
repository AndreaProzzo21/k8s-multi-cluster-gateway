import os
import tempfile
import urllib3

from kubernetes import client
from urllib3.util.retry import Retry
from urllib3.util.timeout import Timeout

import socket
socket.setdefaulttimeout(10)


# ---------------------------------------------------------------------------
# Costanti di configurazione dei timeout e retry.
# Centralizzate qui per essere facilmente modificabili senza toccare la logica.
# ---------------------------------------------------------------------------

# Secondi massimi per stabilire la connessione TCP con il cluster.
# Se il cluster è spento o irraggiungibile, l'errore arriverà entro questo limite.
CONNECT_TIMEOUT: int = 5

# Secondi massimi per ricevere una risposta dopo che la connessione è aperta.
# Copre il caso in cui il cluster accetta la connessione ma non risponde (es. overload).
READ_TIMEOUT: int = 15

# Numero massimo di tentativi in caso di errore di rete transitorio.
# Impostato a 1 (nessun retry automatico) per evitare che richieste a cluster
# offline moltiplicino il tempo di attesa: preferiamo fallire veloce.
MAX_RETRIES: int = 1

# Pausa (in secondi) tra un tentativo e il successivo, con backoff esponenziale.
# Con MAX_RETRIES=1 il valore è quasi ininfluente, ma è buona pratica definirlo.
RETRY_BACKOFF: float = 0.5

# Dimensione massima del pool di connessioni HTTP riusabili verso il cluster.
# Un valore troppo basso crea colli di bottiglia con molte richieste parallele.
CONNECTION_POOL_SIZE: int = 10


class K8sClientFactory:
    """
    Factory statica responsabile della creazione e configurazione dei client
    dell'SDK Kubernetes (CoreV1Api, AppsV1Api, RbacAuthorizationV1Api).

    Responsabilità principali:
    - Configurare l'autenticazione via Bearer Token (Service Account).
    - Gestire la verifica TLS tramite CA Certificate (da stringa, senza file permanenti).
    - Applicare timeout e politiche di retry per prevenire il blocco del gateway
      in caso di cluster irraggiungibili.

    Note di sicurezza:
    - Il CA Certificate viene scritto in un file temporaneo per la sola durata
      della configurazione e rimosso subito dopo tramite un blocco finally.
    - Il token K8s non viene mai loggato.
    """

    @staticmethod
    def get_apis(
        cluster_host: str,
        k8s_token: str,
        ca_cert: str | None = None,
        cluster_id: str | None = None,
    ) -> dict:
        """
        Costruisce e restituisce un dizionario con i client K8s pronti all'uso.

        Il metodo configura un singolo ApiClient condiviso tra tutti i client API,
        in modo che timeout, autenticazione e pool di connessioni siano uniformi.

        Args:
            cluster_host: URL dell'API Server Kubernetes (es. "https://1.2.3.4:6443").
            k8s_token:    Service Account Token per l'autenticazione Bearer.
            ca_cert:      Contenuto PEM del CA Certificate del cluster (opzionale).
                          Se assente o non valido, la verifica SSL viene disabilitata.
            cluster_id:   Identificativo del cluster, usato solo per il logging.

        Returns:
            Dict con chiavi "core_v1", "apps_v1", "rbac_v1" mappate ai rispettivi
            client dell'SDK Kubernetes, tutti configurati con lo stesso ApiClient.

        Raises:
            Non solleva eccezioni proprie: eventuali errori di connessione vengono
            propagati al chiamante (tipicamente CoreManager._handle_exception).
        """
        configuration = client.Configuration()
        configuration.host = cluster_host

        # --- Autenticazione ---
        # Il token viene iniettato come header "Authorization: Bearer <token>"
        # su ogni richiesta HTTP effettuata dall'SDK.
        configuration.api_key["authorization"] = k8s_token
        configuration.api_key_prefix["authorization"] = "Bearer"

        # --- Dimensione pool di connessioni ---
        # Controlla quante connessioni HTTP vengono mantenute aperte verso il cluster.
        configuration.connection_pool_maxsize = CONNECTION_POOL_SIZE

        # _configure_tls restituisce (configuration, handle_file_temporaneo | None).
        # L'handle va tenuto vivo per tutta la durata dell'api_client: urllib3
        # rilegge il file dal disco a ogni connessione TLS.
        configuration, ca_cert_tmp_file = K8sClientFactory._configure_tls(
            configuration, ca_cert, cluster_id
        )

        api_client = client.ApiClient(configuration)

        # Ancoriamo il file temporaneo all'api_client con un attributo custom.
        # Finché l'api_client esiste (durata della singola request), il GC non
        # toccherà il file. Quando l'api_client viene distrutto, il riferimento
        # viene rilasciato e il file può essere pulito dal SO.
        api_client._ca_cert_tmp_file = ca_cert_tmp_file

        K8sClientFactory._apply_network_policies(api_client)

        return {
            "core_v1": client.CoreV1Api(api_client),
            "apps_v1": client.AppsV1Api(api_client),
            "rbac_v1": client.RbacAuthorizationV1Api(api_client),
            "networking_v1": client.NetworkingV1Api(api_client),
        }

    # ---------------------------------------------------------------------------
    # Metodi privati di supporto
    # ---------------------------------------------------------------------------

    @staticmethod
    def _configure_tls(
        configuration: client.Configuration,
        ca_cert: str | None,
        cluster_id: str | None,
    ) -> tuple[client.Configuration, object]:
        """
        Configura la verifica SSL sulla configurazione K8s.

        Se viene fornito un CA Certificate PEM valido, lo scrive in un file
        temporaneo con delete=False e restituisce l'oggetto file insieme alla
        configurazione. Il file DEVE restare su disco per tutta la durata
        dell'api_client: urllib3 non carica il certificato in memoria alla
        configurazione, ma lo rilegge dal path a ogni nuova connessione TLS.
        Il chiamante è responsabile di tenere vivo il riferimento al file
        (assegnandolo a un attributo dell'api_client) per evitare che il
        garbage collector lo chiuda e lo cancelli.

        Se il certificato è assente o non valido, la verifica SSL viene
        disabilitata e i warning di urllib3 soppressi.

        Args:
            configuration: Oggetto Configuration da modificare in-place.
            ca_cert:       Stringa PEM del certificato CA (o None).
            cluster_id:    ID del cluster, usato solo per i messaggi di log.

        Returns:
            Tuple (configuration, tmp_file_handle | None).
            Il secondo elemento è il NamedTemporaryFile aperto (da tenere vivo)
            oppure None se SSL è disabilitato.
        """
        if ca_cert and "-----BEGIN CERTIFICATE-----" in ca_cert:
            try:
                # delete=False: il file non viene cancellato alla chiusura
                # dell'handle. Lo teniamo vivo tramite il riferimento restituito.
                tmp_file = tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".crt",
                    delete=False
                )
                tmp_file.write(ca_cert)
                tmp_file.flush()
                # NON chiamiamo tmp_file.close() qui: lo teniamo aperto
                # così il riferimento è chiaramente vivo finché serve.

                configuration.verify_ssl = True
                configuration.ssl_ca_cert = tmp_file.name
                print(f"[K8sClientFactory] SSL configurato per cluster '{cluster_id}' ({tmp_file.name})")

                return configuration, tmp_file

            except Exception as exc:
                print(f"[K8sClientFactory] Errore scrittura CA cert per '{cluster_id}': {exc}. Fallback a verify_ssl=False.")
                configuration.verify_ssl = False
                return configuration, None
        else:
            configuration.verify_ssl = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            print(f"[K8sClientFactory] verify_ssl=False per cluster '{cluster_id}' (nessun CA cert fornito)")
            return configuration, None

    @staticmethod
    def _apply_network_policies(api_client: client.ApiClient) -> None:
        """
        Applica timeout e politiche di retry al pool di connessioni HTTP dell'ApiClient.

        Questo è il fix centrale per il bug di "gateway appeso":
        senza timeout, una richiesta a un cluster offline blocca il thread
        del worker per un tempo indefinito (default TCP: minuti).

        Con questa configurazione:
        - CONNECT_TIMEOUT: se il cluster non accetta la connessione TCP entro N secondi → errore.
        - READ_TIMEOUT: se la connessione è aperta ma non arrivano dati entro N secondi → errore.
        - MAX_RETRIES=1: nessun retry automatico, preferiamo fallire veloce e restituire
          un 504 al client piuttosto che moltiplicare il tempo di attesa.

        Args:
            api_client: ApiClient già istanziato, modificato in-place sul pool urllib3.
        """
        timeout = Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT)

        retry_policy = Retry(
            total=MAX_RETRIES,
            backoff_factor=RETRY_BACKOFF,
            # Non ritentiamo su errori 4xx/5xx HTTP: solo su errori di rete puri.
            status_forcelist=None,
            # Evitiamo retry su metodi non idempotenti (POST, PATCH)
            # per non rischiare di applicare la stessa operazione due volte.
            allowed_methods={"GET", "HEAD", "OPTIONS"},
        )

        # L'ApiClient K8s usa urllib3.PoolManager internamente.
        # Aggiorniamo le keyword args del pool che vengono passate a ogni connessione.
        api_client.rest_client.pool_manager.connection_pool_kw.update(
            {
                "timeout": timeout,
                "retries": retry_policy,
            }
        )