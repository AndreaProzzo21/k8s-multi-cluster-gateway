"""
helm_kubeconfig.py
==================

Context manager per la generazione di kubeconfig temporanei destinati a Helm.

Helm non accetta token Bearer e CA cert come argomenti diretti sulla CLI:
richiede un file kubeconfig valido. Questo modulo genera il file in-process,
lo scrive su disco con permessi restrittivi, e garantisce la rimozione al
termine del blocco ``with``, anche in caso di eccezione.

Sicurezza
---------
- Il file viene creato con permessi ``0o600`` tramite ``os.open`` con flag
  espliciti, eliminando la finestra tra creazione e chmod di ``open()`` standard.
- Il nome del file include il cluster_id sanitizzato e un suffisso random
  (``tempfile.mkstemp``) per evitare collisioni in ambienti multi-tenant
  con richieste concorrenti sullo stesso cluster.
- Il token K8s non viene mai loggato.
- La pulizia avviene nel blocco ``finally``: il file non sopravvive alla request.
"""

import base64
import os
import tempfile
from contextlib import contextmanager

import yaml


@contextmanager
def temp_kubeconfig(
    cluster_host: str,
    k8s_token: str,
    ca_cert: str,
    cluster_id: str,
):
    """
    Context manager che genera un kubeconfig temporaneo per Helm e lo rimuove
    al termine del blocco ``with``.

    Il kubeconfig generato configura un singolo context che punta al cluster
    specificato, autenticato tramite Bearer token e verifica TLS via CA cert.

    Parameters
    ----------
    cluster_host : str
        URL dell'API Server K8s (es. ``"https://10.0.0.1:6443"``).
    k8s_token : str
        Bearer token del Service Account. Non viene loggato.
    ca_cert : str
        Contenuto PEM del CA Certificate. Viene codificato in base64
        prima di essere inserito nel kubeconfig (formato atteso da kubectl/helm).
    cluster_id : str
        Identificativo del cluster. Usato come nome nel kubeconfig e come
        prefisso del file temporaneo.

    Yields
    ------
    str
        Path assoluto del file kubeconfig temporaneo.

    Raises
    ------
    OSError
        Se la scrittura del file su disco fallisce.

    Examples
    --------
    ::

        with temp_kubeconfig(host, token, ca, "K3S-PROD") as kube_path:
            result = subprocess.run(
                ["helm", "list", "--kubeconfig", kube_path],
                capture_output=True,
            )
    """
    # Sanitizza cluster_id per uso sicuro nel nome file
    safe_id = "".join(c for c in cluster_id if c.isalnum() or c in "-_") or "unknown"

    # Il CA cert PEM deve essere base64-encoded nel kubeconfig
    ca_cert_b64 = base64.b64encode(ca_cert.encode()).decode()

    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "preferences": {},
        "clusters": [
            {
                "name": safe_id,
                "cluster": {
                    "server": cluster_host,
                    "certificate-authority-data": ca_cert_b64,
                },
            }
        ],
        "users": [
            {
                "name": safe_id,
                "user": {
                    "token": k8s_token,
                },
            }
        ],
        "contexts": [
            {
                "name": safe_id,
                "context": {
                    "cluster": safe_id,
                    "user": safe_id,
                },
            }
        ],
        "current-context": safe_id,
    }

    # mkstemp crea il file con O_CREAT | O_EXCL — nessuna race condition
    # sul nome. Il prefisso garantisce identificabilità nei log di sistema.
    fd, path = tempfile.mkstemp(
        suffix=".yaml",
        prefix=f"helm_kube_{safe_id}_",
    )

    try:
        # Impostiamo i permessi prima di scrivere: il fd è già aperto,
        # fchmod è atomico rispetto al file descriptor.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            yaml.dump(kubeconfig, f, default_flow_style=False)
        yield path
    finally:
        # Rimozione garantita anche in caso di eccezione nel blocco with.
        # Errori di rimozione vengono silenziati: il file è in /tmp,
        # verrà rimosso dal SO al riavvio del container al più tardi.
        try:
            os.remove(path)
        except OSError:
            pass