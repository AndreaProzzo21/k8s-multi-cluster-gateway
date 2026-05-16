"""
audit_engine.py
===============

Motore di compliance per la valutazione delle audit rules sul fleet.

Architettura
------------
Le regole sono oggetti ``AuditRule`` registrati nel dizionario ``RULE_REGISTRY``.
Ogni regola dichiara:

- ``id``          — chiave univoca, usata come ``rule_id`` nel DB.
- ``name``        — nome leggibile mostrato nella UI.
- ``description`` — spiegazione di cosa verifica la regola.
- ``severity``    — impatto se la regola fallisce: ``critical``, ``warning``, ``info``.
- ``needs``       — set di chiavi dati richieste nel cluster snapshot.
                    Usato dallo scanner per sapere cosa raccogliere.
- ``evaluate``    — funzione ``(cluster_data: dict) -> AuditFinding`` che
                    esegue la valutazione e restituisce il risultato.

Logica default-on
-----------------
Se nel DB non esiste un record ``AuditRuleConfig`` per una coppia
(cluster_id, rule_id), la regola viene considerata **abilitata**.
Questo garantisce che un cluster appena registrato sia subito sottoposto
all'intera suite di audit senza configurazione manuale.

Aggiungere una nuova regola
---------------------------
1. Definire una funzione ``evaluate(cluster: dict) -> AuditFinding``.
2. Creare un'istanza ``AuditRule`` con i campi richiesti.
3. Registrarla in ``RULE_REGISTRY``.
Nessuna migration DB necessaria.

Dati disponibili dallo scanner
------------------------------
Il cluster snapshot fornito a ``run_audit`` ha questa struttura::

    {
        "cluster_id":   str,
        "cluster_name": str,
        "host":         str,
        "status":       "online" | "offline" | "degraded",
        "server_version": str,          # es. "1.30"
        "error":        str | None,
        "nodes": [
            {
                "name":             str,
                "status":           str,   # "Ready" | altro
                "role":             str,   # "Control Plane" | "Worker"
                "version":          str,   # es. "v1.30.14"
                "os":               str,
                "cpu":              str,   # numero come stringa
                "memory":           str,   # Ki come stringa
                "cpu_allocatable":  str,
                "mem_allocatable":  str,
            }
        ],
        "namespaces": {
            "can_list": bool,
            "items": [
                {"name": str, "status": str}
            ]
        },
        "stats": {
            "cpu_total":    int,
            "pods_total":   int,
            "pods_running": int,
            "pods_failed":  int,
            "namespaces_total": int,
        }
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.infrastructure.database import AuditRuleConfig, SessionLocal


# ---------------------------------------------------------------------------
# Strutture dati
# ---------------------------------------------------------------------------

@dataclass
class AuditFinding:
    """
    Risultato della valutazione di una singola regola su un singolo cluster.

    ``passed`` è True se il cluster soddisfa il requisito della regola.
    ``detail`` contiene una descrizione leggibile del risultato, inclusi
    dettagli specifici (es. nomi dei nodi non ready, versione rilevata).
    ``evidence`` è un dizionario opzionale con dati strutturati per la UI
    (es. lista di namespace senza quota, lista di nodi con versione outdated).
    """
    passed:   bool
    detail:   str
    evidence: dict = field(default_factory=dict)


@dataclass
class AuditRule:
    """
    Definizione di una regola di compliance.

    Attributes
    ----------
    id          : Chiave univoca della regola. Corrisponde a ``rule_id`` nel DB.
    name        : Nome breve mostrato nella UI.
    description : Spiegazione dettagliata di cosa verifica e perché.
    severity    : Impatto del fallimento: ``"critical"``, ``"warning"``, ``"info"``.
    needs       : Set di chiavi del cluster snapshot necessarie per la valutazione.
                  Usato dallo scanner per raccogliere solo i dati richiesti
                  dalle regole attive, evitando chiamate K8s inutili.
    evaluate    : Funzione di valutazione. Riceve il dizionario completo del
                  cluster snapshot e restituisce un ``AuditFinding``.
                  Non deve mai sollevare eccezioni: i casi di dati mancanti
                  vanno gestiti internamente con un finding appropriato.
    """
    id:          str
    name:        str
    description: str
    severity:    str
    needs:       set[str]
    evaluate:    Callable[[dict], AuditFinding]


# ---------------------------------------------------------------------------
# Namespace di sistema — esclusi dalle regole sui namespace utente
# ---------------------------------------------------------------------------

_SYSTEM_NAMESPACES = frozenset({
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "kube-flannel",        # CNI comune in cluster bare-metal
    "kube-proxy",
    "cert-manager",        # spesso considerato infrastruttura
})

# Versione minima K8s supportata — regola k8s-version-policy
_MIN_K8S_MINOR = 28   # K8s 1.28 — EOL Ottobre 2024, sotto questa soglia è critico


# ---------------------------------------------------------------------------
# Funzioni di valutazione delle regole
# ---------------------------------------------------------------------------

def _eval_cluster_reachable(cluster: dict) -> AuditFinding:
    """
    Il cluster deve essere raggiungibile e rispondere alle chiamate API.
    Un cluster offline non può essere auditato su nessun'altra dimensione.
    """
    if cluster.get("status") == "online":
        return AuditFinding(passed=True, detail="Cluster raggiungibile e API server responsivo.")

    error = cluster.get("error") or "Nessun dettaglio disponibile."
    return AuditFinding(
        passed=False,
        detail=f"Cluster non raggiungibile: {error}",
        evidence={"error": error, "host": cluster.get("host")},
    )


def _eval_all_nodes_ready(cluster: dict) -> AuditFinding:
    """
    Tutti i nodi del cluster devono essere in stato Ready.
    Un nodo non Ready indica problemi di risorse, rete o kubelet.
    """
    nodes = cluster.get("nodes") or []
    if not nodes:
        return AuditFinding(
            passed=False,
            detail="Nessun nodo rilevato — impossibile verificare lo stato.",
        )

    not_ready = [n["name"] for n in nodes if n.get("status") != "Ready"]
    if not not_ready:
        return AuditFinding(
            passed=True,
            detail=f"Tutti i {len(nodes)} nodi sono in stato Ready.",
            evidence={"total_nodes": len(nodes)},
        )

    return AuditFinding(
        passed=False,
        detail=f"{len(not_ready)} nodo/i non Ready: {', '.join(not_ready)}",
        evidence={"not_ready_nodes": not_ready, "total_nodes": len(nodes)},
    )


def _eval_k8s_version_policy(cluster: dict) -> AuditFinding:
    """
    Tutti i nodi devono eseguire una versione K8s superiore alla soglia minima.
    Versioni EOL espongono il cluster a vulnerabilità non patchate.
    """
    nodes = cluster.get("nodes") or []
    if not nodes:
        return AuditFinding(passed=False, detail="Nessun nodo disponibile per verifica versione.")

    outdated = []
    for node in nodes:
        version_str = node.get("version", "")
        # Formato atteso: "v1.30.14" → minor = 30
        try:
            parts = version_str.lstrip("v").split(".")
            minor = int(parts[1]) if len(parts) >= 2 else 0
            if minor < _MIN_K8S_MINOR:
                outdated.append({"node": node["name"], "version": version_str})
        except (ValueError, IndexError):
            # Versione non parsabile — la segnaliamo come warning
            outdated.append({"node": node["name"], "version": version_str or "unknown"})

    if not outdated:
        # Prendi la versione dal primo nodo come rappresentativa
        sample_version = nodes[0].get("version", "N/A")
        return AuditFinding(
            passed=True,
            detail=f"Tutti i nodi eseguono K8s >= 1.{_MIN_K8S_MINOR} (rilevato: {sample_version}).",
            evidence={"min_required": f"1.{_MIN_K8S_MINOR}"},
        )

    return AuditFinding(
        passed=False,
        detail=f"{len(outdated)} nodo/i con versione K8s inferiore a 1.{_MIN_K8S_MINOR}.",
        evidence={"outdated_nodes": outdated, "min_required": f"1.{_MIN_K8S_MINOR}"},
    )


def _eval_no_failed_pods(cluster: dict) -> AuditFinding:
    """
    Nessun pod deve trovarsi in stato Failed o assimilabile.
    Pod in stato Failed indicano errori applicativi o di scheduling non risolti.
    """
    stats = cluster.get("stats") or {}
    failed = stats.get("pods_failed", 0)
    total  = stats.get("pods_total", 0)

    if failed == 0:
        return AuditFinding(
            passed=True,
            detail=f"Nessun pod in stato Failed su {total} pod totali.",
            evidence={"pods_total": total, "pods_failed": 0},
        )

    return AuditFinding(
        passed=False,
        detail=f"{failed} pod in stato Failed su {total} totali.",
        evidence={"pods_total": total, "pods_failed": failed},
    )


def _eval_pod_health_ratio(cluster: dict) -> AuditFinding:
    """
    Almeno l'80% dei pod deve essere in stato Running.
    Un ratio basso indica problemi di stabilità del cluster o delle applicazioni.
    """
    stats   = cluster.get("stats") or {}
    total   = stats.get("pods_total", 0)
    running = stats.get("pods_running", 0)

    if total == 0:
        return AuditFinding(passed=True, detail="Nessun pod presente nel cluster.")

    ratio = (running / total) * 100
    threshold = 80.0

    if ratio >= threshold:
        return AuditFinding(
            passed=True,
            detail=f"{running}/{total} pod Running ({ratio:.0f}%) — sopra la soglia dell'{threshold:.0f}%.",
            evidence={"pods_running": running, "pods_total": total, "ratio_pct": round(ratio, 1)},
        )

    return AuditFinding(
        passed=False,
        detail=f"Solo {running}/{total} pod Running ({ratio:.0f}%) — sotto la soglia dell'{threshold:.0f}%.",
        evidence={"pods_running": running, "pods_total": total, "ratio_pct": round(ratio, 1)},
    )


def _eval_user_namespaces_present(cluster: dict) -> AuditFinding:
    """
    Il cluster deve avere almeno un namespace utente (non di sistema).
    Un cluster senza namespace utente non ha workload applicativi deployati.
    """
    ns_data = cluster.get("namespaces") or {}

    if not ns_data.get("can_list", True):
        return AuditFinding(
            passed=True,
            detail="Permessi insufficienti per listare i namespace — regola saltata.",
        )

    items    = ns_data.get("items") or []
    user_ns  = [ns["name"] for ns in items if ns.get("name") not in _SYSTEM_NAMESPACES]

    if user_ns:
        return AuditFinding(
            passed=True,
            detail=f"{len(user_ns)} namespace utente trovati: {', '.join(user_ns)}.",
            evidence={"user_namespaces": user_ns},
        )

    return AuditFinding(
        passed=False,
        detail="Nessun namespace utente trovato — solo namespace di sistema presenti.",
        evidence={"system_namespaces": [ns["name"] for ns in items]},
    )


def _eval_namespace_count_reasonable(cluster: dict) -> AuditFinding:
    """
    Il numero di namespace utente non deve superare una soglia ragionevole.
    Un numero eccessivo di namespace può indicare mancanza di governance.
    La soglia è fissata a 50 namespace utente — configurabile in futuro.
    """
    _MAX_USER_NAMESPACES = 50

    ns_data  = cluster.get("namespaces") or {}
    items    = ns_data.get("items") or []
    user_ns  = [ns["name"] for ns in items if ns.get("name") not in _SYSTEM_NAMESPACES]
    count    = len(user_ns)

    if count <= _MAX_USER_NAMESPACES:
        return AuditFinding(
            passed=True,
            detail=f"{count} namespace utente — entro la soglia di {_MAX_USER_NAMESPACES}.",
            evidence={"count": count, "threshold": _MAX_USER_NAMESPACES},
        )

    return AuditFinding(
        passed=False,
        detail=f"{count} namespace utente superano la soglia di {_MAX_USER_NAMESPACES}.",
        evidence={"count": count, "threshold": _MAX_USER_NAMESPACES, "namespaces": user_ns},
    )


def _eval_control_plane_present(cluster: dict) -> AuditFinding:
    """
    Deve essere presente almeno un nodo Control Plane nel cluster.
    Cluster senza Control Plane identificato indicano problemi di configurazione
    del gateway o di labeling dei nodi.
    """
    nodes = cluster.get("nodes") or []
    cp_nodes = [n["name"] for n in nodes if n.get("role") == "Control Plane"]

    if cp_nodes:
        return AuditFinding(
            passed=True,
            detail=f"Control Plane identificato: {', '.join(cp_nodes)}.",
            evidence={"control_plane_nodes": cp_nodes},
        )

    return AuditFinding(
        passed=False,
        detail="Nessun nodo con ruolo Control Plane rilevato.",
        evidence={"total_nodes": len(nodes)},
    )


def _eval_worker_nodes_present(cluster: dict) -> AuditFinding:
    """
    Deve essere presente almeno un nodo Worker.
    Un cluster con solo Control Plane non può ospitare workload applicativi.
    """
    nodes       = cluster.get("nodes") or []
    worker_nodes = [n["name"] for n in nodes if n.get("role") == "Worker"]

    if worker_nodes:
        return AuditFinding(
            passed=True,
            detail=f"{len(worker_nodes)} Worker node/i trovati: {', '.join(worker_nodes)}.",
            evidence={"worker_nodes": worker_nodes},
        )

    return AuditFinding(
        passed=False,
        detail="Nessun Worker node rilevato — il cluster non può schedulare workload applicativi.",
        evidence={"total_nodes": len(nodes)},
    )


def _eval_os_homogeneity(cluster: dict) -> AuditFinding:
    """
    Tutti i nodi devono eseguire lo stesso OS.
    Ambienti eterogenei aumentano la complessità operativa e il rischio di
    comportamenti inconsistenti tra nodi.
    """
    nodes = cluster.get("nodes") or []
    if not nodes:
        return AuditFinding(passed=False, detail="Nessun nodo disponibile per verifica OS.")

    os_set = set(n.get("os", "unknown") for n in nodes)

    if len(os_set) == 1:
        return AuditFinding(
            passed=True,
            detail=f"Tutti i nodi eseguono lo stesso OS: {next(iter(os_set))}.",
            evidence={"os": next(iter(os_set))},
        )

    # Mappa nodo → OS per l'evidence
    node_os_map = {n["name"]: n.get("os", "unknown") for n in nodes}
    return AuditFinding(
        passed=False,
        detail=f"OS eterogenei rilevati: {', '.join(sorted(os_set))}.",
        evidence={"os_distribution": node_os_map},
    )

def _eval_node_cpu_pressure(cluster: dict) -> AuditFinding:
    """Monitora la saturazione della CPU sui nodi."""
    nodes = cluster.get("nodes") or []
    stressed = []
    for n in nodes:
        try:
            total = int(n.get("cpu", "0"))
            alloc = int(n.get("cpu_allocatable", "0"))
            if total > 0:
                usage = ((total - alloc) / total) * 100
                if usage > 85: stressed.append(f"{n['name']} ({usage:.0f}%)")
        except: continue
    return AuditFinding(passed=len(stressed)==0, 
                        detail="CPU disponibile su tutti i nodi." if not stressed else f"Nodi sotto sforzo CPU (>85%): {', '.join(stressed)}",
                        evidence={"stressed_nodes": stressed})

def _eval_namespace_quota_presence(cluster: dict) -> AuditFinding:
    """Verifica se i namespace utente sono protetti da ResourceQuotas."""
    ns_data = cluster.get("namespaces", {})
    items = ns_data.get("items", [])
    user_ns = [ns["name"] for ns in items if ns["name"] not in _SYSTEM_NAMESPACES]
    # Nota: assume che lo scanner popoli 'has_quota' (puoi aggiungerlo allo scanner k8s)
    missing = [ns for ns in user_ns if not any(n["name"] == ns and n.get("has_quota") for n in items)]
    return AuditFinding(passed=len(missing)==0,
                        detail="Tutti i namespace hanno quote di risorse." if not missing else f"Namespace senza limiti (rischioso): {', '.join(missing)}",
                        evidence={"missing_quotas": missing})

def _eval_loadbalancer_usage(cluster: dict) -> AuditFinding:
    """Controlla l'uso di Service LoadBalancer (spesso costosi o limitati)."""
    stats = cluster.get("stats", {})
    lb_count = stats.get("services_lb", 0)
    limit = 10 
    return AuditFinding(passed=lb_count <= limit,
                        detail=f"Uso LoadBalancer sotto controllo ({lb_count})." if lb_count <= limit else f"Eccessivo uso di LoadBalancer ({lb_count}) — rischio costi/IP esauriti.",
                        evidence={"lb_count": lb_count, "limit": limit})

def _eval_pending_pods_check(cluster: dict) -> AuditFinding:
    """Rileva pod in stato Pending (spesso indice di risorse insufficienti)."""
    stats = cluster.get("stats", {})
    pending = stats.get("pods_pending", 0)
    return AuditFinding(passed=pending == 0,
                        detail="Nessun pod in attesa di scheduling." if pending == 0 else f"Rilevati {pending} pod in stato Pending. Possibile mancanza di risorse o errori di affinità.",
                        evidence={"pods_pending": pending})

def _eval_single_replica_deployments(cluster: dict) -> AuditFinding:
    """Verifica la presenza di workload critici senza alta affidabilità (HA)."""
    # Nota: richiede che lo scanner conti i deployment con replicas=1
    single_replicas = cluster.get("stats", {}).get("deployments_single_replica", 0)
    return AuditFinding(passed=single_replicas == 0,
                        detail="Tutti i workload hanno repliche multiple (HA)." if single_replicas == 0 else f"Rilevati {single_replicas} deployment con singola replica. Rischio downtime durante update.",
                        evidence={"single_replica_count": single_replicas})

def _eval_deprecated_api_usage(cluster: dict) -> AuditFinding:
    """Controlla se ci sono API deprecate rispetto alla versione del server."""
    # Semplificato: se la versione è >= 1.29 e ci sono vecchi oggetti
    ver = cluster.get("server_version", "0.0")
    has_old = cluster.get("stats", {}).get("deprecated_apis", False)
    passed = not (float(ver) >= 1.29 and has_old)
    return AuditFinding(passed=passed,
                        detail="Nessuna API deprecata rilevata." if passed else "Rilevato uso di API deprecate (es. Beta Ingress/Autoscaling) incompatibili con K8s 1.29+.")


# ---------------------------------------------------------------------------
# Registry delle regole
# ---------------------------------------------------------------------------
# Le regole sono ordinate per severità (critical → warning → info) e poi
# per categoria logica. L'ordine determina l'ordine di visualizzazione nella UI.

RULE_REGISTRY: dict[str, AuditRule] = {r.id: r for r in [

    # ── Disponibilità (Critical) ─────────────────────────────────────────

    AuditRule(
        id="cluster-reachable",
        name="Cluster Reachable",
        description=(
            "Verifica che il cluster sia raggiungibile e che l'API server risponda. "
            "Un cluster offline non può essere auditato su nessun'altra dimensione. "
            "Cause comuni: rete non disponibile, VPN non connessa, cluster spento."
        ),
        severity="critical",
        needs={"status", "error"},
        evaluate=_eval_cluster_reachable,
    ),

    AuditRule(
        id="all-nodes-ready",
        name="All Nodes Ready",
        description=(
            "Tutti i nodi del cluster devono essere in stato Ready. "
            "Un nodo NotReady indica problemi al kubelet, alla rete o alle risorse "
            "del nodo stesso (memoria, disco, CPU pressure)."
        ),
        severity="critical",
        needs={"nodes"},
        evaluate=_eval_all_nodes_ready,
    ),

    AuditRule(
        id="control-plane-present",
        name="Control Plane Node Present",
        description=(
            "Deve essere identificabile almeno un nodo Control Plane. "
            "L'assenza indica un problema di labeling dei nodi o di configurazione "
            "del profilo admin usato dallo scanner."
        ),
        severity="critical",
        needs={"nodes"},
        evaluate=_eval_control_plane_present,
    ),

    # ── Workload (Warning) ───────────────────────────────────────────────

    AuditRule(
        id="no-failed-pods",
        name="No Failed Pods",
        description=(
            "Nessun pod deve trovarsi in stato Failed. "
            "Pod in stato Failed indicano errori applicativi non gestiti, "
            "problemi di scheduling o risorse insufficienti."
        ),
        severity="warning",
        needs={"stats"},
        evaluate=_eval_no_failed_pods,
    ),

    AuditRule(
        id="pod-health-ratio",
        name="Pod Health Ratio ≥ 80%",
        description=(
            "Almeno l'80% dei pod deve essere in stato Running. "
            "Un ratio inferiore indica instabilità delle applicazioni "
            "o problemi di risorse nel cluster."
        ),
        severity="warning",
        needs={"stats"},
        evaluate=_eval_pod_health_ratio,
    ),

    AuditRule(
        id="worker-nodes-present",
        name="Worker Nodes Present",
        description=(
            "Il cluster deve avere almeno un Worker node. "
            "Un cluster con solo Control Plane non può ospitare workload applicativi "
            "in configurazioni standard."
        ),
        severity="warning",
        needs={"nodes"},
        evaluate=_eval_worker_nodes_present,
    ),

    # ── Versioning (Warning) ─────────────────────────────────────────────

    AuditRule(
        id="k8s-version-policy",
        name=f"K8s Version ≥ 1.{_MIN_K8S_MINOR}",
        description=(
            f"Tutti i nodi devono eseguire Kubernetes >= 1.{_MIN_K8S_MINOR}. "
            "Versioni EOL non ricevono patch di sicurezza e potrebbero essere "
            "incompatibili con componenti aggiornati (CNI, CSI, admission controllers)."
        ),
        severity="warning",
        needs={"nodes"},
        evaluate=_eval_k8s_version_policy,
    ),

    AuditRule(
        id="os-homogeneity",
        name="Homogeneous Node OS",
        description=(
            "Tutti i nodi devono eseguire lo stesso sistema operativo. "
            "Ambienti eterogenei aumentano la complessità operativa e il rischio "
            "di comportamenti inconsistenti (syscall, cgroup versioni, kernel features)."
        ),
        severity="warning",
        needs={"nodes"},
        evaluate=_eval_os_homogeneity,
    ),

    # ── Governance (Info) ────────────────────────────────────────────────

    AuditRule(
        id="user-namespaces-present",
        name="User Namespaces Present",
        description=(
            "Il cluster deve avere almeno un namespace non di sistema. "
            "Un cluster senza namespace utente non ha workload applicativi deployati "
            "e potrebbe indicare un cluster non ancora configurato."
        ),
        severity="info",
        needs={"namespaces"},
        evaluate=_eval_user_namespaces_present,
    ),

    AuditRule(
        id="namespace-count-reasonable",
        name="Namespace Count ≤ 50",
        description=(
            "Il numero di namespace utente non deve superare 50. "
            "Un numero eccessivo può indicare mancanza di governance o "
            "un processo di cleanup non attivo."
        ),
        severity="info",
        needs={"namespaces"},
        evaluate=_eval_namespace_count_reasonable,
    ),

    # ── Nuove Regole Observer (Monitoring & Cost) ────────────────────────

    AuditRule(
        id="node-cpu-pressure",
        name="Node CPU Pressure < 85%",
        description="Monitora se i nodi stanno saturando la capacità di calcolo.",
        severity="warning",
        needs={"nodes"},
        evaluate=_eval_node_cpu_pressure,
    ),

    AuditRule(
        id="pending-pods-check",
        name="No Pending Pods",
        description="Rileva pod che non riescono a essere schedulati sui nodi.",
        severity="critical",
        needs={"stats"},
        evaluate=_eval_pending_pods_check,
    ),

    AuditRule(
        id="namespace-quota-presence",
        name="Namespace Resource Quotas",
        description="Assicura che ogni namespace utente abbia limiti di risorse per evitare 'Noisy Neighbor'.",
        severity="warning",
        needs={"namespaces"},
        evaluate=_eval_namespace_quota_presence,
    ),

    AuditRule(
        id="loadbalancer-limit",
        name="LoadBalancer Usage Control",
        description="Monitora il numero di servizi di tipo LoadBalancer per controllo costi e risorse cloud.",
        severity="info",
        needs={"stats"},
        evaluate=_eval_loadbalancer_usage,
    ),

    AuditRule(
        id="ha-workload-policy",
        name="High Availability Deployment",
        description="Verifica che i deployment abbiano più di una replica per garantire la continuità del servizio.",
        severity="info",
        needs={"stats"},
        evaluate=_eval_single_replica_deployments,
    ),

    AuditRule(
        id="deprecated-api-check",
        name="Modern API Compliance",
        description="Rileva l'uso di API Kubernetes deprecate o rimosse nelle versioni recenti.",
        severity="warning",
        needs={"server_version", "stats"},
        evaluate=_eval_deprecated_api_usage,
    ),

]}


# ---------------------------------------------------------------------------
# Funzioni pubbliche
# ---------------------------------------------------------------------------

def get_all_rules() -> list[dict]:
    """
    Restituisce la lista di tutte le regole disponibili nel registry.
    Usata dagli endpoint admin per mostrare le regole configurabili.

    Returns
    -------
    list[dict]
        Lista di dizionari con id, name, description, severity.
        Non include la funzione evaluate (non serializzabile in JSON).
    """
    return [
        {
            "id":          rule.id,
            "name":        rule.name,
            "description": rule.description,
            "severity":    rule.severity,
            "needs":       list(rule.needs),
        }
        for rule in RULE_REGISTRY.values()
    ]


def get_active_rules_for_cluster(cluster_id: str) -> list[AuditRule]:
    """
    Restituisce le regole attive per un cluster, tenendo conto della
    configurazione nel DB (logica default-on).

    Algoritmo:
    1. Legge tutti i record ``AuditRuleConfig`` per questo cluster.
    2. Per ogni regola nel registry:
       - Se esiste un record con ``enabled=False`` → regola disabilitata.
       - Altrimenti (record assente o ``enabled=True``) → regola attiva.

    Parameters
    ----------
    cluster_id : str
        ID del cluster (es. "K3S").

    Returns
    -------
    list[AuditRule]
        Lista delle regole da eseguire su questo cluster.
    """
    db = SessionLocal()
    try:
        configs = db.query(AuditRuleConfig).filter(
            AuditRuleConfig.cluster_id == cluster_id
        ).all()
        # Costruisce mappa rule_id → enabled
        config_map: dict[str, bool] = {c.rule_id: c.enabled for c in configs}
    finally:
        db.close()

    return [
        rule for rule in RULE_REGISTRY.values()
        # default-on: se non c'è config, la regola è abilitata
        if config_map.get(rule.id, True)
    ]


def get_rule_config_for_cluster(cluster_id: str) -> list[dict]:
    """
    Restituisce la configurazione completa di tutte le regole per un cluster,
    includendo sia le regole con config esplicita nel DB sia quelle con default.

    Usata dall'endpoint ``GET /admin/audit/rules/{cluster_id}`` per mostrare
    nella UI lo stato di ogni regola con il toggle abilitato/disabilitato.

    Returns
    -------
    list[dict]
        Lista di dizionari con id, name, description, severity, enabled, note.
        ``enabled`` riflette la config DB o il default (True) se assente.
        ``note`` è None se non è stata impostata una nota dall'admin.
    """
    db = SessionLocal()
    try:
        configs = db.query(AuditRuleConfig).filter(
            AuditRuleConfig.cluster_id == cluster_id
        ).all()
        config_map: dict[str, AuditRuleConfig] = {c.rule_id: c for c in configs}
    finally:
        db.close()

    result = []
    for rule in RULE_REGISTRY.values():
        db_config = config_map.get(rule.id)
        result.append({
            "id":          rule.id,
            "name":        rule.name,
            "description": rule.description,
            "severity":    rule.severity,
            # default-on: True se non c'è config esplicita
            "enabled":     db_config.enabled if db_config else True,
            "note":        db_config.note    if db_config else None,
        })
    return result


def set_rule_config(cluster_id: str, rule_id: str, enabled: bool, note: str | None = None) -> dict:
    """
    Abilita o disabilita una regola per un cluster specifico (upsert).

    Se esiste già un record per (cluster_id, rule_id) lo aggiorna.
    Se non esiste lo crea. Usa merge di SQLAlchemy per l'upsert.

    Parameters
    ----------
    cluster_id : str
        ID del cluster target.
    rule_id : str
        ID della regola da configurare. Deve esistere nel ``RULE_REGISTRY``.
    enabled : bool
        True per abilitare, False per disabilitare.
    note : str | None
        Motivazione opzionale (es. "cluster di sviluppo").

    Returns
    -------
    dict
        Configurazione aggiornata con cluster_id, rule_id, enabled, note.

    Raises
    ------
    ValueError
        Se ``rule_id`` non esiste nel registry.
    """
    if rule_id not in RULE_REGISTRY:
        raise ValueError(
            f"Regola '{rule_id}' non trovata nel registry. "
            f"Regole disponibili: {', '.join(RULE_REGISTRY.keys())}"
        )

    db = SessionLocal()
    try:
        # Cerca record esistente
        config = db.query(AuditRuleConfig).filter(
            AuditRuleConfig.cluster_id == cluster_id,
            AuditRuleConfig.rule_id    == rule_id,
        ).first()

        if config:
            config.enabled = enabled
            config.note    = note
        else:
            config = AuditRuleConfig(
                cluster_id=cluster_id,
                rule_id=rule_id,
                enabled=enabled,
                note=note,
            )
            db.add(config)

        db.commit()
        db.refresh(config)

        return {
            "cluster_id": config.cluster_id,
            "rule_id":    config.rule_id,
            "enabled":    config.enabled,
            "note":       config.note,
        }
    finally:
        db.close()


def run_audit(fleet_data: list[dict], respect_config: bool = True) -> list[dict]:
    """
    Esegue le audit rules su tutti i cluster della fleet.

    Per ogni cluster recupera le regole attive (rispettando la configurazione
    nel DB se ``respect_config=True``) ed esegue ogni regola, raccogliendo
    i finding. I cluster offline ricevono solo la regola ``cluster-reachable``
    per evitare falsi negativi su regole che richiedono dati non disponibili.

    Parameters
    ----------
    fleet_data : list[dict]
        Lista di snapshot cluster prodotti da ``scan_all_clusters()``.
    respect_config : bool
        Se True (default), usa ``get_active_rules_for_cluster()`` per filtrare
        le regole in base alla configurazione DB.
        Se False, esegue tutte le regole su tutti i cluster (utile per test).

    Returns
    -------
    list[dict]
        Lista di risultati per cluster::

            [
                {
                    "cluster_id":   str,
                    "cluster_name": str,
                    "status":       str,
                    "score":        int,   # regole passate
                    "total":        int,   # regole valutate
                    "score_pct":    float, # percentuale di compliance
                    "findings": [
                        {
                            "rule_id":   str,
                            "rule_name": str,
                            "severity":  str,
                            "passed":    bool,
                            "detail":    str,
                            "evidence":  dict,
                        }
                    ]
                }
            ]
    """
    results = []

    for cluster in fleet_data:
        cluster_id = cluster["cluster_id"]
        is_offline = cluster.get("status") == "offline"

        # Determina le regole da eseguire
        if respect_config:
            active_rules = get_active_rules_for_cluster(cluster_id)
        else:
            active_rules = list(RULE_REGISTRY.values())

        # Cluster offline: esegui solo la regola di raggiungibilità
        # per evitare falsi negativi ("no nodes" quando il cluster è spento)
        if is_offline:
            reachable_rule = RULE_REGISTRY.get("cluster-reachable")
            active_rules   = [reachable_rule] if reachable_rule else []

        findings = []
        for rule in active_rules:
            try:
                finding = rule.evaluate(cluster)
            except Exception as exc:
                # La funzione evaluate non dovrebbe mai sollevare,
                # ma gestiamo il caso in modo difensivo.
                finding = AuditFinding(
                    passed=False,
                    detail=f"Errore interno durante la valutazione: {exc}",
                )

            findings.append({
                "rule_id":   rule.id,
                "rule_name": rule.name,
                "severity":  rule.severity,
                "passed":    finding.passed,
                "detail":    finding.detail,
                "evidence":  finding.evidence,
            })

        passed = sum(1 for f in findings if f["passed"])
        total  = len(findings)

        results.append({
            "cluster_id":   cluster_id,
            "cluster_name": cluster.get("cluster_name", cluster_id),
            "status":       cluster.get("status", "unknown"),
            "score":        passed,
            "total":        total,
            "score_pct":    round((passed / total * 100) if total else 0, 1),
            "findings":     findings,
        })

    return results