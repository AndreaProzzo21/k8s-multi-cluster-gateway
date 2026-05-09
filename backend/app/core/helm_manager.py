"""
helm_manager.py
===============

Manager per operazioni Helm su cluster Kubernetes remoti.

Architettura
------------
Tutte le operazioni Helm vengono eseguite tramite il binario ``helm`` CLI
installato nel container del gateway. Ogni metodo pubblico costruisce i
parametri del comando, esegue ``helm`` come sottoprocesso asincrono tramite
``asyncio.create_subprocess_exec``, e restituisce un dizionario strutturato.

Perché subprocess e non una libreria Python
-------------------------------------------
Al momento della scrittura non esiste una libreria Python che wrappa Helm 3
con supporto completo e manutenzione attiva:

- ``pyhelm`` supporta solo Helm 2 (EOL dal novembre 2020).
- ``pyhelm3`` è un wrapper non ufficiale attorno a subprocess, con API
  instabile e nessuna garanzia di compatibilità con le versioni future di Helm.

L'approccio subprocess garantisce:
1. Compatibilità con tutte le versioni di Helm 3.x.
2. Accesso a tutte le funzionalità CLI (incluse quelle non ancora nelle SDK).
3. Lo stesso comportamento che avrebbe un operatore che usa ``helm`` da terminale.

Gestione timeout
----------------
Ogni operazione ha un timeout configurabile. I default riflettono la natura
delle operazioni:

- ``list``, ``status``, ``history``, ``search``: 30s (lettura, veloci)
- ``install/upgrade``, ``uninstall``: 120s (scrittura, attendono ready)
- ``repo add/update``: 60s (dipende dalla rete)
- ``install_from_zip``: 120s (+ tempo di estrazione locale)

Il timeout scatta su ``asyncio.wait_for`` e invia SIGKILL al processo figlio
tramite ``proc.kill()``.

Sicurezza
---------
- Il kubeconfig è un file temporaneo con permessi 0o600 (vedi ``helm_kubeconfig.py``).
- Il token K8s non viene mai loggato.
- Il cluster_id è sanitizzato prima dell'uso come componente di path.
- I file temporanei (valori YAML, chart ZIP estratti) vengono rimossi nel
  blocco ``finally`` di ogni metodo che li crea.
- Non vengono mai eseguiti comandi costruiti con interpolazione di stringhe
  non sanitizzate: tutti gli argomenti vengono passati come lista a
  ``create_subprocess_exec``, che non usa shell e non è vulnerabile a
  command injection.
"""

import asyncio
import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Timeout di default per categoria di operazione (secondi)
# ---------------------------------------------------------------------------
TIMEOUT_READ: float = 30.0      # list, status, history, search
TIMEOUT_WRITE: float = 120.0    # install, upgrade, uninstall
TIMEOUT_REPO: float = 60.0      # repo add, repo update
TIMEOUT_WAIT: float = 300.0     # operazioni con --wait (opzionale)


class HelmManager:
    """
    Interfaccia Python per operazioni Helm su un cluster Kubernetes remoto.

    Ogni istanza è associata a un singolo cluster e a un kubeconfig temporaneo
    generato per la request corrente. Non deve essere condivisa tra request.

    Parameters
    ----------
    kubeconfig_path : str
        Path del kubeconfig temporaneo generato da ``temp_kubeconfig``.
        Il file deve esistere per tutta la durata dell'istanza.
    cluster_id : str
        Identificativo del cluster. Usato nei messaggi di log.

    Attributes
    ----------
    helm_available : bool
        True se il binario ``helm`` è presente nel PATH del container.
        Verificato una sola volta al momento dell'istanziazione.
    """

    # In __init__, aggiungi i path per-cluster
    def __init__(self, kubeconfig_path: str, cluster_id: str):
        self._kubeconfig = kubeconfig_path
        self._cluster_id = cluster_id
        self._helm_bin: str | None = shutil.which("helm")
        
        # Directory isolate per questo cluster
        # Ogni cluster ha il suo repositories.yaml e il suo cache indipendente
        base = f"/tmp/helm_repos/{cluster_id}"
        self._repo_config = f"{base}/repositories.yaml"
        self._repo_cache  = f"{base}/cache"
        os.makedirs(self._repo_cache, exist_ok=True)
        # Crea repositories.yaml vuoto se non esiste (helm lo richiede)
        if not os.path.exists(self._repo_config):
            os.makedirs(base, exist_ok=True)
            with open(self._repo_config, "w") as f:
                f.write("apiVersion: \"\"\ngenerated: \"0001-01-01T00:00:00Z\"\nrepositories: []\n")

    @property
    def helm_available(self) -> bool:
        """True se il binario helm è presente nel container."""
        return self._helm_bin is not None

    # ---------------------------------------------------------------------------
    # Metodo interno: esecuzione comandi
    # ---------------------------------------------------------------------------

    async def _run(
        self,
        *args: str,
        timeout: float = TIMEOUT_READ,
        parse_json: bool = False,
    ) -> dict[str, Any]:
        """
        Esegue un comando helm asincrono e restituisce il risultato strutturato.

        Costruisce il comando aggiungendo sempre ``--kubeconfig`` come primo
        argomento dopo ``helm``, in modo che tutte le operazioni puntino al
        cluster corretto. Il flag ``--output json`` viene aggiunto automaticamente
        quando ``parse_json=True``.

        Parameters
        ----------
        *args : str
            Argomenti del comando helm (es. ``"list"``, ``"-n"``, ``"default"``).
            Non devono includere ``--kubeconfig``: viene aggiunto automaticamente.
        timeout : float
            Secondi prima di inviare SIGKILL al processo helm.
        parse_json : bool
            Se True, aggiunge ``--output json`` al comando e tenta il parsing
            di stdout come JSON, aggiungendo il risultato al dizionario
            di ritorno sotto la chiave ``"data"``.

        Returns
        -------
        dict
            Dizionario con le chiavi:
            - ``"success"`` (bool): True se returncode == 0.
            - ``"returncode"`` (int): exit code del processo.
            - ``"stdout"`` (str): output standard (stripped).
            - ``"stderr"`` (str): output di errore (stripped).
            - ``"data"`` (list | dict | None): JSON parsato se ``parse_json=True``
              e il parsing ha avuto successo; None altrimenti.
            - ``"command"`` (str): rappresentazione leggibile del comando eseguito,
              senza il path del kubeconfig (sicurezza).

        Raises
        ------
        RuntimeError
            Se ``helm_available`` è False (controllo difensivo: la dependency
            dovrebbe aver già bloccato la request in questo caso).
        asyncio.TimeoutError
            Rilanciata dopo SIGKILL al processo, per gestione upstream.
        """
        if not self._helm_bin:
            raise RuntimeError("Binario helm non disponibile nel container.")

        cmd_args = list(args)

        if parse_json and "--output" not in cmd_args and "-o" not in cmd_args:
            cmd_args.extend(["--output", "json"])

        full_cmd = [
            self._helm_bin,
            "--kubeconfig",        self._kubeconfig,
            "--repository-config", self._repo_config,   # ← isolamento per cluster
            "--repository-cache",  self._repo_cache,    # ← isolamento per cluster
            *cmd_args,
        ]

        # Per i log: mostriamo il comando senza il path del kubeconfig
        # (che include cluster_id e path /tmp, ma non il token).
        readable_cmd = f"helm {' '.join(cmd_args)}"
        print(f"[HelmManager:{self._cluster_id}] Esecuzione: {readable_cmd}")

        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()  # drain pipes dopo kill
            print(f"[HelmManager:{self._cluster_id}] TIMEOUT: {readable_cmd}")
            raise

        stdout = stdout_bytes.decode(errors="replace").strip()
        stderr = stderr_bytes.decode(errors="replace").strip()
        success = proc.returncode == 0

        result: dict[str, Any] = {
            "success": success,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "data": None,
            "command": readable_cmd,
        }

        if parse_json and success and stdout:
            try:
                result["data"] = json.loads(stdout)
            except json.JSONDecodeError as exc:
                print(
                    f"[HelmManager:{self._cluster_id}] "
                    f"Parsing JSON fallito per '{readable_cmd}': {exc}"
                )

        if not success:
            print(
                f"[HelmManager:{self._cluster_id}] "
                f"Errore (rc={proc.returncode}): {stderr[:200]}"
            )

        return result

    # ---------------------------------------------------------------------------
    # Release: operazioni CRUD
    # ---------------------------------------------------------------------------

    async def list_releases(self, namespace: str | None = None) -> dict:
        """
        Elenca le release Helm in un namespace o in tutti i namespace.

        Parameters
        ----------
        namespace : str | None
            Se fornito, elenca solo le release nel namespace specificato.
            Se None, elenca le release in tutti i namespace (``--all-namespaces``).

        Returns
        -------
        dict
            Dizionario con ``"data"`` contenente la lista delle release,
            ognuna con: name, namespace, revision, updated, status, chart, app_version.
        """
        args = ["list"]
        if namespace:
            args.extend(["-n", namespace])
        else:
            args.append("--all-namespaces")
        result = await self._run(*args, parse_json=True, timeout=TIMEOUT_READ)
        # helm list --output json restituisce "null" invece di "[]" su namespace
        # vuoto in alcune versioni. Normalizziamo sempre a lista.
        if result["success"] and result["data"] is None:
            result["data"] = []
        return result

    async def install_or_upgrade(
        self,
        release_name: str,
        chart_ref: str,
        namespace: str = "default",
        values: dict | None = None,
        version: str | None = None,
        create_namespace: bool = False,
        atomic: bool = False,
        wait: bool = False,
        timeout_seconds: int = 300,
    ) -> dict:
        """
        Esegue ``helm upgrade --install`` (crea se non esiste, aggiorna se esiste).

        Parameters
        ----------
        release_name : str
            Nome della release Helm (es. ``"my-nginx"``).
        chart_ref : str
            Riferimento al chart: ``"repo/chart"`` (es. ``"bitnami/nginx"``),
            path locale, o URL OCI (``"oci://registry/chart"``).
        namespace : str
            Namespace di destinazione. Default: ``"default"``.
        values : dict | None
            Valori di override per il chart (equivalente a ``-f values.yaml``).
            Vengono scritti in un file temporaneo e passati con ``-f``.
        version : str | None
            Versione specifica del chart (es. ``"1.2.3"``). Se None usa la latest.
        create_namespace : bool
            Se True aggiunge ``--create-namespace``. Default: True.
        atomic : bool
            Se True aggiunge ``--atomic``: rollback automatico in caso di errore.
        wait : bool
            Se True aggiunge ``--wait``: attende che tutte le risorse siano Ready.
        timeout_seconds : int
            Timeout passato a ``--timeout`` (secondi). Usato solo se ``wait=True``.

        Returns
        -------
        dict
            Risultato del comando con stdout/stderr. In caso di successo,
            stdout contiene il summary della release in JSON.
        """
        args = ["upgrade", "--install", release_name, chart_ref, "-n", namespace]

        if create_namespace:
            args.append("--create-namespace")
        if version:
            args.extend(["--version", version])
        if atomic:
            args.append("--atomic")
        if wait:
            args.extend(["--wait", "--timeout", f"{timeout_seconds}s"])

        # I valori di override vengono scritti su un file temporaneo con 0o600.
        values_file: str | None = None
        if values:
            fd, values_file = tempfile.mkstemp(suffix=".yaml", prefix="helm_values_")
            try:
                import yaml
                with os.fdopen(fd, "w") as f:
                    yaml.dump(values, f, default_flow_style=False)
                os.chmod(values_file, 0o600)
            except OSError:
                try:
                    os.remove(values_file)
                except OSError:
                    pass
                raise
            args.extend(["-f", values_file])

        op_timeout = TIMEOUT_WAIT if (wait or atomic) else TIMEOUT_WRITE
        try:
            return await self._run(*args, timeout=op_timeout, parse_json=True)
        finally:
            if values_file:
                try:
                    os.remove(values_file)
                except OSError:
                    pass

    async def uninstall(
        self,
        release_name: str,
        namespace: str = "default",
        keep_history: bool = False,
    ) -> dict:
        """
        Rimuove una release Helm dal cluster.

        Parameters
        ----------
        release_name : str
            Nome della release da rimuovere.
        namespace : str
            Namespace della release.
        keep_history : bool
            Se True aggiunge ``--keep-history``: preserva la storia della
            release nel cluster per permettere ``rollback`` futuro.

        Returns
        -------
        dict
            Risultato con stdout del messaggio di conferma Helm.
        """
        args = ["uninstall", release_name, "-n", namespace]
        if keep_history:
            args.append("--keep-history")
        return await self._run(*args, timeout=TIMEOUT_WRITE)

    async def get_release_status(
        self,
        release_name: str,
        namespace: str = "default",
    ) -> dict:
        """
        Restituisce lo stato dettagliato di una release.

        Equivalente a ``helm status <release> -n <namespace> --output json``.
        Include: info, chart, config (valori applicati), manifest (YAML delle risorse).

        Returns
        -------
        dict
            Con ``"data"`` contenente il JSON completo dello status Helm.
        """
        return await self._run(
            "status", release_name, "-n", namespace,
            parse_json=True,
            timeout=TIMEOUT_READ,
        )

    async def get_release_history(
        self,
        release_name: str,
        namespace: str = "default",
        max_revisions: int = 10,
    ) -> dict:
        """
        Restituisce la storia delle revisioni di una release.

        Parameters
        ----------
        max_revisions : int
            Numero massimo di revisioni da restituire (``--max``). Default: 10.

        Returns
        -------
        dict
            Con ``"data"`` contenente la lista delle revisioni: revision,
            updated, status, chart, app_version, description.
        """
        return await self._run(
            "history", release_name,
            "-n", namespace,
            "--max", str(max_revisions),
            parse_json=True,
            timeout=TIMEOUT_READ,
        )

    async def rollback(
        self,
        release_name: str,
        revision: int,
        namespace: str = "default",
        wait: bool = False,
    ) -> dict:
        """
        Esegue il rollback di una release a una revisione precedente.

        Parameters
        ----------
        revision : int
            Numero di revisione target. Passare 0 per tornare alla revisione
            precedente (comportamento nativo di Helm).
        wait : bool
            Se True aggiunge ``--wait``.

        Returns
        -------
        dict
            Risultato con stdout del messaggio di conferma Helm.
        """
        args = ["rollback", release_name, str(revision), "-n", namespace]
        if wait:
            args.append("--wait")
        op_timeout = TIMEOUT_WAIT if wait else TIMEOUT_WRITE
        return await self._run(*args, timeout=op_timeout)

    async def get_release_values(
        self,
        release_name: str,
        namespace: str = "default",
        all_values: bool = False,
    ) -> dict:
        """
        Restituisce i valori applicati a una release.

        Parameters
        ----------
        all_values : bool
            Se True aggiunge ``--all``: restituisce tutti i valori inclusi
            quelli di default del chart, non solo gli override.

        Returns
        -------
        dict
            Con ``"data"`` contenente il dizionario dei valori in JSON.
        """
        args = ["get", "values", release_name, "-n", namespace]
        if all_values:
            args.append("--all")
        return await self._run(*args, parse_json=True, timeout=TIMEOUT_READ)

    # ---------------------------------------------------------------------------
    # Repository
    # ---------------------------------------------------------------------------

    async def repo_add(self, name: str, url: str) -> dict:
        """
        Aggiunge un repository Helm.

        Usa ``--force-update`` per aggiornare l'URL se il repo esiste già
        con un URL diverso, invece di fallire.

        Parameters
        ----------
        name : str
            Nome locale del repo (es. ``"bitnami"``).
        url : str
            URL del repository (es. ``"https://charts.bitnami.com/bitnami"``).

        Returns
        -------
        dict
            Risultato con stdout del messaggio di conferma.
        """
        return await self._run(
            "repo", "add", name, url, "--force-update",
            timeout=TIMEOUT_REPO,
        )

    async def repo_update(self) -> dict:
        """
        Aggiorna l'indice di tutti i repository configurati.

        Equivalente a ``helm repo update``. Da eseguire dopo ``repo_add``
        per rendere disponibili i chart del nuovo repository.

        Returns
        -------
        dict
            Risultato con stdout del messaggio di aggiornamento.
        """
        return await self._run("repo", "update", timeout=TIMEOUT_REPO)

    async def repo_list(self) -> dict:
        """
        Elenca i repository Helm configurati nel kubeconfig corrente.

        Returns
        -------
        dict
            Con ``"data"`` contenente la lista dei repo: name, url.
            Lista vuota se nessun repository è configurato.
        """
        result = await self._run("repo", "list", parse_json=True, timeout=TIMEOUT_READ)
        # `helm repo list` esce con rc=1 e stderr "no repositories configured"
        # quando non c'è nessun repo. Non è un errore applicativo: normalizziamo.
        if not result["success"] and "no repositories" in result.get("stderr", "").lower():
            result["success"] = True
            result["data"] = []
        return result

    async def search_repo(self, query: str, versions: bool = False) -> dict:
        """
        Cerca chart nei repository configurati.

        Parameters
        ----------
        query : str
            Termine di ricerca (es. ``"nginx"``, ``"bitnami/redis"``).
        versions : bool
            Se True aggiunge ``--versions``: mostra tutte le versioni disponibili.

        Returns
        -------
        dict
            Con ``"data"`` contenente la lista dei chart trovati:
            name, chart_version, app_version, description.
        """
        args = ["search", "repo", query]
        if versions:
            args.append("--versions")
        return await self._run(*args, parse_json=True, timeout=TIMEOUT_READ)

    async def repo_remove(self, name: str) -> dict:
        """
        Rimuove un repository Helm per nome.
    
        Equivalente a ``helm repo remove <name>``.
        Helm restituisce rc=1 se il repository non esiste — normalizzato
        in HTTP 404 dal router tramite _require_success.
    
        Parameters
        ----------
        name : str
            Nome locale del repository da rimuovere (es. ``"mosquitto"``).
    
        Returns
        -------
        dict
            Risultato standard con success/stdout/stderr.
        """
        return await self._run("repo", "remove", name, timeout=TIMEOUT_REPO)

    async def show_chart_values(self, chart_ref: str, version: str | None = None) -> dict:
        """
        Mostra i valori di default di un chart (equivale a ``helm show values``).

        Utile per il frontend: permette di mostrare i parametri configurabili
        prima di eseguire un install.

        Parameters
        ----------
        chart_ref : str
            Riferimento al chart (``"bitnami/nginx"``, path locale, OCI URL).
        version : str | None
            Versione specifica. Se None usa la latest.

        Returns
        -------
        dict
            Con ``"stdout"`` contenente il YAML dei valori di default.
            Non usa ``--output json`` perché ``helm show values`` restituisce YAML.
        """
        args = ["show", "values", chart_ref]
        if version:
            args.extend(["--version", version])
        return await self._run(*args, timeout=TIMEOUT_READ)

    # ---------------------------------------------------------------------------
    # Install da ZIP
    # ---------------------------------------------------------------------------

    async def install_from_zip(
        self,
        zip_bytes: bytes,
        release_name: str,
        namespace: str = "default",
        values: dict | None = None,
        create_namespace: bool = False,
        atomic: bool = False,
        wait: bool = False,
    ) -> dict:
        """
        Installa un Helm chart fornito come archivio ZIP.

        Il metodo estrae il contenuto dello ZIP in una directory temporanea,
        individua il chart cercando ``Chart.yaml``, esegue ``helm upgrade --install``
        sul path estratto, e rimuove la directory temporanea nel blocco ``finally``.

        Parameters
        ----------
        zip_bytes : bytes
            Contenuto binario del file ZIP contenente il chart Helm.
            Lo ZIP deve contenere una directory con ``Chart.yaml`` al suo interno.
        release_name : str
            Nome della release Helm.
        namespace : str
            Namespace di destinazione. Default: ``"default"``.
        values : dict | None
            Valori di override (passati a ``install_or_upgrade``).
        create_namespace : bool
            Se True crea il namespace se non esiste.
        atomic : bool
            Se True rollback automatico in caso di errore.
        wait : bool
            Se True attende che tutte le risorse siano Ready.

        Returns
        -------
        dict
            Risultato di ``install_or_upgrade`` sul chart estratto.

        Raises
        ------
        ValueError
            Se lo ZIP non contiene nessun ``Chart.yaml`` (chart non valido).
        zipfile.BadZipFile
            Se i bytes forniti non sono un archivio ZIP valido.
        """
        tmpdir = tempfile.mkdtemp(prefix=f"helm_chart_{self._cluster_id}_")
        try:
            # Estrazione ZIP
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zf.extractall(tmpdir)

            # Ricerca del chart: il primo Chart.yaml trovato in profondità
            chart_path: str | None = None
            for chart_yaml in sorted(Path(tmpdir).rglob("Chart.yaml")):
                chart_path = str(chart_yaml.parent)
                break

            if chart_path is None:
                raise ValueError(
                    "Chart.yaml non trovato nell'archivio ZIP. "
                    "Assicurarsi che lo ZIP contenga un Helm chart valido."
                )

            print(
                f"[HelmManager:{self._cluster_id}] "
                f"Chart trovato in: {chart_path} — "
                f"release: '{release_name}', namespace: '{namespace}'"
            )

            return await self.install_or_upgrade(
                release_name=release_name,
                chart_ref=chart_path,
                namespace=namespace,
                values=values,
                create_namespace=create_namespace,
                atomic=atomic,
                wait=wait,
            )

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def lint(
        self,
        chart_ref: str | None = None,
        zip_bytes: bytes | None = None,
        values: dict | None = None,
        strict: bool = False,
    ) -> dict:
        """
        Esegue ``helm lint`` su un chart da repo/path o da archivio ZIP.

        helm lint analizza il chart per errori di sintassi, template non validi,
        valori mancanti e best practice. Non installa nulla nel cluster.

        Parameters
        ----------
        chart_ref : str | None
            Riferimento al chart (``"bitnami/nginx"``, path locale).
            Mutuamente esclusivo con ``zip_bytes``.
        zip_bytes : bytes | None
            Contenuto binario ZIP del chart. Se fornito, viene estratto in
            una directory temporanea e lint viene eseguito sul path estratto.
        values : dict | None
            Valori di override da passare a lint con ``-f``.
        strict : bool
            Se True aggiunge ``--strict``: tratta i warning come errori.

        Returns
        -------
        dict
            Risultato standard. ``stdout`` contiene l'output di lint con
            eventuali warning/error per chart e template.
            ``success`` è True anche se ci sono warning (solo False su errori).
        """
        tmpdir: str | None = None
        values_file: str | None = None

        try:
            # Determina il target del lint
            if zip_bytes is not None:
                tmpdir = tempfile.mkdtemp(prefix=f"helm_lint_{self._cluster_id}_")
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    zf.extractall(tmpdir)
                # Trova Chart.yaml
                target: str | None = None
                for chart_yaml in sorted(Path(tmpdir).rglob("Chart.yaml")):
                    target = str(chart_yaml.parent)
                    break
                if target is None:
                    raise ValueError("Chart.yaml non trovato nell'archivio ZIP.")
            elif chart_ref:
                target = chart_ref
            else:
                raise ValueError("Fornire chart_ref oppure zip_bytes.")

            args = ["lint", target]

            if strict:
                args.append("--strict")

            # Valori di override
            if values:
                fd, values_file = tempfile.mkstemp(suffix=".yaml", prefix="helm_lint_vals_")
                import yaml
                with os.fdopen(fd, "w") as f:
                    yaml.dump(values, f, default_flow_style=False)
                os.chmod(values_file, 0o600)
                args.extend(["-f", values_file])

            # helm lint restituisce rc=1 se trova errori, rc=0 se solo warning.
            # NON usiamo parse_json: lint non supporta --output json.
            # Usiamo _run senza check su success: vogliamo sempre stdout+stderr.
            result = await self._run(*args, timeout=TIMEOUT_READ, parse_json=False)

            # Normalizziamo: aggiungiamo campo "has_errors" e "has_warnings" per il frontend
            stdout = result.get("stdout", "")
            result["has_errors"]   = "[ERROR]" in stdout or not result["success"]
            result["has_warnings"] = "[WARNING]" in stdout
            # Forziamo success=True se l'unico problema sono warning (rc=0)
            # In questo modo il frontend può distinguere warning da errori
            return result

        finally:
            if values_file:
                try: os.remove(values_file)
                except OSError: pass
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)