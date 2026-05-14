"""
audit_routes.py
===============

Router FastAPI per il sistema di compliance audit del gateway.

Tutti gli endpoint richiedono l'header ``X-Admin-Key`` tramite la
dependency ``require_admin_key``. Nessun JWT utente è accettato su
queste route — sono riservate all'amministratore del gateway.

Prefisso in main.py
-------------------
    app.include_router(audit_router, prefix="/api/v1/admin/audit", tags=["audit"])

Endpoint esposti
----------------
GET  /rules
    Lista tutte le regole disponibili nel registry (definite in audit_engine.py).
    Non dipende dal DB — restituisce sempre il catalogo completo.

GET  /rules/{cluster_id}
    Configurazione per-cluster di tutte le regole, con stato enabled/disabled
    e nota opzionale. Include sia le regole con config esplicita nel DB
    sia quelle con default (enabled=True).

PATCH /rules/{cluster_id}/{rule_id}
    Abilita o disabilita una singola regola per un cluster.
    Crea o aggiorna il record in audit_rule_configs (upsert).
    Accetta opzionalmente una nota motivazionale.

POST /rules/{cluster_id}/reset
    Rimuove tutta la configurazione esplicita per un cluster,
    riportando tutte le regole al default (enabled=True).
    Utile quando si vuole resettare la configurazione di un cluster
    appena migrato o riconfigurato.

GET  /results
    Esegue l'audit sull'intera fleet usando i dati della cache FleetManager.
    Se la cache è vuota forza una scansione sincrona.
    Rispetta la configurazione per-cluster (regole disabilitate non vengono eseguite).

GET  /results/{cluster_id}
    Come /results ma per un singolo cluster. Utile per drill-down
    sulla pagina di dettaglio di un cluster specifico.

POST /results/refresh
    Forza una scansione della fleet e restituisce i risultati freschi.
    Equivale a GET /results?force=true ma come POST per chiarezza semantica
    (ha side effect: aggiorna la cache globale del FleetManager).
"""

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.api.dependencies.get_admin_key import require_admin_key
from app.core.audit_engine import (
    get_all_rules,
    get_rule_config_for_cluster,
    run_audit,
    set_rule_config,
    RULE_REGISTRY,
)
from app.core.fleet_manager import FleetManager
from app.infrastructure.database import AuditRuleConfig, SessionLocal

audit_router = APIRouter()


# ---------------------------------------------------------------------------
# GET /rules — catalogo completo delle regole disponibili
# ---------------------------------------------------------------------------

@audit_router.get(
    "/rules",
    dependencies=[Depends(require_admin_key)],
    summary="Lista tutte le audit rules disponibili",
)
def list_all_rules():
    """
    Restituisce il catalogo completo delle audit rules definite nel registry.

    Non dipende dal DB né dalla configurazione per-cluster — è il catalogo
    statico delle regole implementate nel codice. Usato dal frontend per
    costruire la UI di configurazione.

    Response
    --------
    Lista di oggetti con: id, name, description, severity, needs.
    """
    return get_all_rules()


# ---------------------------------------------------------------------------
# GET /rules/{cluster_id} — configurazione per-cluster
# ---------------------------------------------------------------------------

@audit_router.get(
    "/rules/{cluster_id}",
    dependencies=[Depends(require_admin_key)],
    summary="Configurazione delle audit rules per un cluster",
)
def get_cluster_rule_config(cluster_id: str):
    """
    Restituisce la configurazione di tutte le regole per il cluster specificato.

    Per ogni regola include:
    - ``enabled``: True se la regola è attiva (default) o se è stata
      esplicitamente abilitata; False se è stata disabilitata dall'admin.
    - ``note``: motivazione opzionale della disabilitazione.

    Le regole senza configurazione esplicita nel DB appaiono con
    ``enabled=True`` (logica default-on).

    Parameters
    ----------
    cluster_id : str
        ID del cluster (es. "K3S", "DIPI-1"). Case-insensitive.
    """
    return get_rule_config_for_cluster(cluster_id.upper())


# ---------------------------------------------------------------------------
# PATCH /rules/{cluster_id}/{rule_id} — abilita/disabilita una regola
# ---------------------------------------------------------------------------

@audit_router.patch(
    "/rules/{cluster_id}/{rule_id}",
    dependencies=[Depends(require_admin_key)],
    summary="Abilita o disabilita una regola per un cluster",
)
def update_rule_config(
    cluster_id: str,
    rule_id:    str,
    enabled: bool = Body(..., description="True per abilitare, False per disabilitare"),
    note: str | None = Body(
        None,
        description="Motivazione opzionale (es. 'cluster di sviluppo — privileged pods accettati')",
    ),
):
    """
    Abilita o disabilita una singola audit rule per un cluster (upsert).

    Se non esiste un record per (cluster_id, rule_id) lo crea.
    Se esiste già lo aggiorna. Il campo ``note`` è opzionale — se None
    viene mantenuto il valore precedente solo se il record esisteva;
    su un nuovo record viene impostato a None.

    Parameters
    ----------
    cluster_id : str
        ID del cluster target.
    rule_id : str
        ID della regola da configurare. Deve esistere nel registry.
    enabled : bool
        Stato desiderato della regola.
    note : str | None
        Motivazione opzionale della configurazione.

    Raises
    ------
    HTTPException 404
        Se ``rule_id`` non esiste nel registry.
    """
    if rule_id not in RULE_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Regola '{rule_id}' non trovata nel registry. "
                f"Regole disponibili: {', '.join(RULE_REGISTRY.keys())}"
            ),
        )

    try:
        result = set_rule_config(
            cluster_id=cluster_id.upper(),
            rule_id=rule_id,
            enabled=enabled,
            note=note,
        )
        return {
            "status":     "updated",
            "cluster_id": result["cluster_id"],
            "rule_id":    result["rule_id"],
            "enabled":    result["enabled"],
            "note":       result["note"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---------------------------------------------------------------------------
# POST /rules/{cluster_id}/reset — reset alla configurazione default
# ---------------------------------------------------------------------------

@audit_router.post(
    "/rules/{cluster_id}/reset",
    dependencies=[Depends(require_admin_key)],
    summary="Ripristina la configurazione default per un cluster",
    status_code=status.HTTP_200_OK,
)
def reset_cluster_rule_config(cluster_id: str):
    """
    Rimuove tutta la configurazione esplicita delle regole per il cluster,
    riportando ogni regola al suo stato default (enabled=True).

    Questo non elimina il cluster né i profili — rimuove solo i record
    della tabella ``audit_rule_configs`` per questo cluster.

    Utile quando:
    - Un cluster viene riconfigurato e si vuole ricominciare da zero.
    - La configurazione è diventata inconsistente e si vuole ripristinare.

    Parameters
    ----------
    cluster_id : str
        ID del cluster da resettare.
    """
    db = SessionLocal()
    try:
        deleted = db.query(AuditRuleConfig).filter(
            AuditRuleConfig.cluster_id == cluster_id.upper()
        ).delete()
        db.commit()
        return {
            "status":     "reset",
            "cluster_id": cluster_id.upper(),
            "deleted":    deleted,
            "message":    (
                f"Rimossi {deleted} record di configurazione. "
                "Tutte le regole tornano al default (enabled=True)."
            ),
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /results — audit sull'intera fleet
# ---------------------------------------------------------------------------

@audit_router.get(
    "/results",
    dependencies=[Depends(require_admin_key)],
    summary="Esegui l'audit su tutti i cluster della fleet",
)
async def get_audit_results():
    """
    Esegue le audit rules su tutti i cluster registrati usando i dati
    della cache del FleetManager.

    Se la cache è vuota (primo avvio, container appena ripartito) forza
    una scansione sincrona della fleet prima di procedere con l'audit.
    In questo caso la risposta può richiedere alcuni secondi.

    Le regole disabilitate per un cluster non vengono eseguite (rispetta
    la configurazione in ``audit_rule_configs``).

    I cluster offline ricevono solo la regola ``cluster-reachable``,
    evitando falsi negativi per dati non disponibili.

    Response
    --------
    Lista di risultati per cluster con: cluster_id, cluster_name, status,
    score (regole passate), total (regole valutate), score_pct, findings.
    """
    # FleetManager.get_cached_status() restituisce una LISTA []
    fleet = FleetManager.get_cached_status()

    if not fleet:
        # Cache vuota: forza scan sincrono (ritorna la nuova lista)
        fleet = await FleetManager.refresh()

    # run_audit si aspetta una lista di cluster
    results = run_audit(fleet, respect_config=True)
    
    return {
        "last_updated": "N/A", # Il tuo FleetManager attuale non salva il timestamp, puoi aggiungerlo se vuoi
        "clusters_scanned": len(results),
        "results": results,
        "summary": _build_summary(results),
    }


# ---------------------------------------------------------------------------
# GET /results/{cluster_id} — audit su un singolo cluster
# ---------------------------------------------------------------------------

@audit_router.get(
    "/results/{cluster_id}",
    dependencies=[Depends(require_admin_key)],
    summary="Esegui l'audit su un singolo cluster",
)
async def get_cluster_audit_result(cluster_id: str):
    """
    Esegue le audit rules su un singolo cluster.

    Filtra i dati della cache per il cluster specificato.
    Se il cluster non è trovato nella cache, forza una scansione completa.

    Parameters
    ----------
    cluster_id : str
        ID del cluster (es. "DIPI-1").

    Raises
    ------
    HTTPException 404
        Se il cluster non è trovato nel DB o nella cache.
    """
    fleet = FleetManager.get_cached_status()

    cluster_data = next(
        (c for c in fleet if c["cluster_id"].upper() == cluster_id.upper()),
        None
    )

    if cluster_data is None:
        # Prova con scan fresco
        fleet = await FleetManager.refresh()
        cluster_data = next(
            (c for c in fleet if c["cluster_id"].upper() == cluster_id.upper()),
            None
        )

    if cluster_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cluster '{cluster_id}' non trovato nella fleet.",
        )

    results = run_audit([cluster_data], respect_config=True)
    return results[0] if results else {}


# ---------------------------------------------------------------------------
# POST /results/refresh — scan fresco + audit
# ---------------------------------------------------------------------------

@audit_router.post(
    "/results/refresh",
    dependencies=[Depends(require_admin_key)],
    summary="Forza una scansione della fleet e restituisce i risultati audit freschi",
    status_code=status.HTTP_200_OK,
)
async def refresh_and_audit():
    """
    Forza una scansione completa della fleet, aggiorna la cache del
    FleetManager e restituisce i risultati dell'audit sui dati freschi.

    A differenza di GET /results (che usa la cache), questa route
    esegue sempre chiamate reali ai cluster K8s. Può richiedere
    diversi secondi in funzione del numero di cluster e della latenza.

    Side effect: aggiorna la cache globale del FleetManager, influenzando
    anche le successive chiamate a GET /fleet/status e GET /results.
    """
    try:
        # FleetManager.refresh() ora esiste dopo la nostra modifica precedente
        fleet = await FleetManager.refresh()
        results = run_audit(fleet, respect_config=True)

        return {
            "last_updated": "Just now",
            "clusters_scanned": len(results),
            "results": results,
            "summary": _build_summary(results),
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Audit Refresh failed: {str(e)}"
        )


# ---------------------------------------------------------------------------
# Helper privato
# ---------------------------------------------------------------------------

def _build_summary(results: list[dict]) -> dict:
    """
    Calcola statistiche aggregate sui risultati dell'audit.
    Usato dalla UI per la summary row in cima alla pagina.
    """
    total_clusters = len(results)
    # fully_compliant: cluster dove score == total
    fully_compliant = sum(1 for r in results if r.get("score", 0) == r.get("total", 0) and r.get("total", 0) > 0)
    
    total_findings = sum(r.get("total", 0) for r in results)
    passed_findings = sum(r.get("score", 0) for r in results)
    failed_findings = total_findings - passed_findings

    # Conta i finding critici falliti
    critical_failures = 0
    for r in results:
        for f in r.get("findings", []):
            # Verifichiamo chepassed sia proprio False (non None) e severity sia critical
            if f.get("passed") is False and f.get("severity") == "critical":
                critical_failures += 1

    avg_score = (
        round(passed_findings / total_findings * 100, 1)
        if total_findings > 0 else 0.0
    )

    return {
        "total_clusters": total_clusters,
        "fully_compliant": fully_compliant,
        "total_findings": total_findings,
        "passed_findings": passed_findings,
        "failed_findings": failed_findings,
        "critical_failures": critical_failures,
        "avg_score_pct": avg_score,
    }