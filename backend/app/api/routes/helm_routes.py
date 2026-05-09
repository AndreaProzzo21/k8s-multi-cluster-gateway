# app/api/routes/helm_routes.py
"""
Helm Routes
===========

Tutte le route sono prefissate /helm/* in main.py:
    app.include_router(helm_router, prefix="/api/v1/helm", tags=["helm"])

Convenzioni
-----------
- Namespace-scoped : /namespaces/{namespace}/releases/...
- Cluster-scoped   : /repos/..., /charts/...

Gestione errori
---------------
``_require_success`` è applicato a TUTTE le route (lettura e scrittura).
- Se Helm restituisce rc != 0 con "forbidden"/"unauthorized" → HTTP 403
  → il frontend (apiCall) lo converte in throw Error("RESTRICTED")
  → renderRestrictedAccess() si comporta come nella K8s dashboard
- Qualsiasi altro errore Helm → HTTP 400 con il messaggio stderr
- Nessuna route restituisce mai HTTP 200 con success=false nascosto
"""

import json
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile, status

from app.api.dependencies.get_helm_manager import get_helm_manager
from app.core.helm_manager import HelmManager

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper centrale — applicato a TUTTE le route
# ---------------------------------------------------------------------------

def _require_success(result: dict, operation: str) -> dict:
    """
    Valida il risultato di un comando Helm.

    Logica:
    - success=True  → restituisce result invariato
    - success=False + "forbidden"/"unauthorized" in stderr → HTTP 403
      (il frontend apiCall.js lo intercetta e lancia Error("RESTRICTED"))
    - success=False + altro → HTTP 400 con stderr come detail

    Non solleva mai su success=True, anche se stdout è vuoto
    (es. helm list su namespace senza release → success=True, data=[]).
    """
    if result.get("success"):
        return result

    stderr = (result.get("stderr") or result.get("stdout") or "").strip()

    if "forbidden" in stderr.lower() or "unauthorized" in stderr.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: {stderr[:400]}",
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"{operation} failed: {stderr[:500]}",
    )


# ---------------------------------------------------------------------------
# RELEASES — lettura
# ---------------------------------------------------------------------------

@router.get("/namespaces/{namespace}/releases")
async def list_helm_releases(
    namespace: str,
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Elenca le release Helm nel namespace specificato.

    Risposta in caso di successo:
        { "success": true, "data": [ ...releases... ] }
    Lista vuota se il namespace esiste ma non ha release:
        { "success": true, "data": [] }
    HTTP 403 se il SA non ha permessi sui Secret del namespace.
    """
    result = await manager.list_releases(namespace)
    return _require_success(result, f"helm list -n {namespace}")


@router.get("/namespaces/{namespace}/releases/{release_name}/status")
async def get_release_status(
    namespace: str,
    release_name: str,
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Restituisce stato dettagliato, manifest e note di una release.
    HTTP 400 se la release non esiste.
    """
    result = await manager.get_release_status(release_name, namespace)
    return _require_success(result, f"helm status {release_name}")


@router.get("/namespaces/{namespace}/releases/{release_name}/history")
async def get_release_history(
    namespace: str,
    release_name: str,
    max: int = Query(10, ge=1, le=100, description="Numero massimo di revisioni da restituire"),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Restituisce la cronologia delle revisioni di una release.
    HTTP 400 se la release non esiste.
    """
    result = await manager.get_release_history(release_name, namespace, max)
    return _require_success(result, f"helm history {release_name}")


@router.get("/namespaces/{namespace}/releases/{release_name}/values")
async def get_release_values(
    namespace: str,
    release_name: str,
    all: bool = Query(False, description="Se true, include anche i valori di default del chart"),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Restituisce i valori applicati alla release.
    Con all=false: solo i valori di override (quelli passati con -f o --set).
    Con all=true: tutti i valori, compresi i default del chart.
    """
    result = await manager.get_release_values(release_name, namespace, all)
    return _require_success(result, f"helm get values {release_name}")


# ---------------------------------------------------------------------------
# RELEASES — scrittura
# ---------------------------------------------------------------------------

@router.post("/namespaces/{namespace}/releases/{release_name}", status_code=status.HTTP_200_OK)
async def install_or_upgrade_chart(
    namespace: str,
    release_name: str,
    chart_ref: str = Query(
        ...,
        description="Riferimento al chart: 'repo/chart' (es. bitnami/nginx), path locale, OCI URL",
    ),
    version: Optional[str] = Query(
        None,
        description="Versione specifica del chart. Se omesso usa la latest.",
    ),
    create_namespace: bool = Query(
        False,
        description="Il namespace viene recuperato. Se non esiste va creato da SA che ha il permesso e non indipendentemente.",
    ),
    atomic: bool = Query(
        False,
        description="Rollback automatico se il deploy fallisce (--atomic)",
    ),
    wait: bool = Query(
        False,
        description="Attende che tutte le risorse siano Ready prima di rispondere (--wait)",
    ),
    timeout_seconds: int = Query(
        300, ge=10, le=600,
        description="Timeout per --wait/--atomic in secondi",
    ),
    # Body JSON opzionale: valori di override.
    # FastAPI lo deserializza quando Content-Type: application/json.
    # Endpoint separato (from-zip) gestisce multipart/form-data.
    values: dict[str, Any] = Body(
        default={},
        description="Valori di override (equivalente a -f values.yaml)",
    ),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Installa o aggiorna una release Helm (``helm upgrade --install``).
    Se la release non esiste viene creata; se esiste viene aggiornata.
    """
    result = await manager.install_or_upgrade(
        release_name=release_name,
        chart_ref=chart_ref,
        namespace=namespace,
        values=values or None,
        version=version,
        create_namespace=create_namespace,
        atomic=atomic,
        wait=wait,
        timeout_seconds=timeout_seconds,
    )
    return _require_success(result, f"helm upgrade --install {release_name}")


@router.post(
    "/namespaces/{namespace}/releases/{release_name}/from-zip",
    status_code=status.HTTP_200_OK,
)
async def install_from_zip(
    namespace: str,
    release_name: str,
    file: UploadFile = File(
        ...,
        description="Archivio ZIP con il chart Helm (deve contenere Chart.yaml)",
    ),
    values_json: Optional[str] = Query(
        None,
        description="Valori di override come JSON string (es: '{\"replicaCount\": 2}')",
    ),
    atomic: bool = Query(False),
    wait: bool = Query(False),
    create_namespace: bool = Query(False),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Installa un chart da archivio ZIP.
    Il file ZIP deve contenere una directory con ``Chart.yaml``.
    Esegue ``helm upgrade --install`` sul chart estratto.
    """
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Il file deve avere estensione .zip",
        )

    content = await file.read()

    values: dict | None = None
    if values_json:
        try:
            values = json.loads(values_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"values_json non è un JSON valido: {exc}",
            )

    result = await manager.install_from_zip(
        zip_bytes=content,
        release_name=release_name,
        namespace=namespace,
        values=values,
        atomic=atomic,
        wait=wait,
        create_namespace=create_namespace
    )
    return _require_success(result, f"helm install from zip → {release_name}")


@router.post(
    "/namespaces/{namespace}/releases/{release_name}/rollback",
    status_code=status.HTTP_200_OK,
)
async def rollback_release(
    namespace: str,
    release_name: str,
    revision: int = Query(
        0, ge=0,
        description="Revisione target. 0 = revisione precedente (comportamento nativo Helm)",
    ),
    wait: bool = Query(False),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Rollback di una release a una revisione specifica.
    ``revision=0`` equivale a tornare alla revisione precedente.
    """
    result = await manager.rollback(release_name, revision, namespace, wait)
    return _require_success(result, f"helm rollback {release_name} {revision}")


@router.delete(
    "/namespaces/{namespace}/releases/{release_name}",
    status_code=status.HTTP_200_OK,
)
async def uninstall_release(
    namespace: str,
    release_name: str,
    keep_history: bool = Query(
        False,
        description="Se true, preserva la storia della release per rollback futuri",
    ),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Rimuove una release Helm e tutte le risorse K8s gestite da essa.
    Con keep_history=true la storia rimane per permettere rollback.
    """
    result = await manager.uninstall(release_name, namespace, keep_history)
    return _require_success(result, f"helm uninstall {release_name}")


# ---------------------------------------------------------------------------
# REPOSITORIES
# ---------------------------------------------------------------------------

@router.get("/repos")
async def list_repos(
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Elenca i repository Helm configurati.
    Restituisce { "success": true, "data": [] } se nessun repo è configurato
    (helm repo list restituisce rc=1 in quel caso: normalizzato nel manager).
    """
    result = await manager.repo_list()
    # repo_list normalizza già rc=1 "no repositories" → success=True, data=[]
    # Per qualsiasi altro errore (es. filesystem) propaghiamo comunque.
    return _require_success(result, "helm repo list")


@router.post("/repos", status_code=status.HTTP_200_OK)
async def add_repo(
    name: str = Query(..., description="Nome locale del repository (es. 'bitnami')"),
    url: str = Query(..., description="URL del repository Helm"),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Aggiunge un repository Helm.
    Se esiste già con URL diverso lo aggiorna (--force-update).
    """
    result = await manager.repo_add(name, url)
    return _require_success(result, f"helm repo add {name}")


@router.post("/repos/update", status_code=status.HTTP_200_OK)
async def update_repos(
    manager: HelmManager = Depends(get_helm_manager),
):
    """Aggiorna l'indice locale di tutti i repository configurati."""
    result = await manager.repo_update()
    return _require_success(result, "helm repo update")


@router.delete("/repos/{name}", status_code=status.HTTP_200_OK)
async def remove_repo(
    name: str,
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Rimuove un repository Helm per nome (``helm repo remove``).
 
    Non esposto nel frontend — utilizzabile via /docs.
    HTTP 404 se il repository non esiste.
    """
    result = await manager.repo_remove(name)
 
    if not result.get("success"):
        stderr = (result.get("stderr") or "").strip()
        # Helm dice "no repo named X found" quando il nome non esiste
        if "no repo" in stderr.lower() or "not found" in stderr.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Repository '{name}' not found.",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"helm repo remove {name} failed: {stderr[:400]}",
        )
 
    return result


# ---------------------------------------------------------------------------
# CHARTS
# ---------------------------------------------------------------------------

@router.get("/charts/search")
async def search_charts(
    q: str = Query(..., description="Termine di ricerca (es. 'nginx', 'bitnami/redis')"),
    versions: bool = Query(False, description="Mostra tutte le versioni disponibili del chart"),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Cerca chart nei repository configurati (helm search repo).
    HTTP 400 se nessun repository è configurato (helm restituisce errore).
    Lista vuota se nessun chart corrisponde alla query.
    """
    result = await manager.search_repo(q, versions)
    _require_success(result, f"helm search repo {q}")
    # Normalizza data null → [] (helm search restituisce null su zero risultati)
    if result["data"] is None:
        result["data"] = []
    return result


@router.get("/charts/values")
async def get_chart_default_values(
    chart_ref: str = Query(
        ...,
        description="Riferimento chart (es. 'bitnami/nginx', 'oci://registry/chart')",
    ),
    version: Optional[str] = Query(
        None,
        description="Versione specifica. Se omesso usa la latest.",
    ),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Mostra i valori di default di un chart (helm show values).
    Restituisce YAML grezzo in ``stdout`` — non JSON.
    Usato dal frontend per mostrare i parametri configurabili prima del deploy.
    """
    result = await manager.show_chart_values(chart_ref, version)
    return _require_success(result, f"helm show values {chart_ref}")

@router.get("/charts/lint")
async def lint_chart_from_ref(
    chart_ref: str = Query(..., description="Riferimento chart (es. 'bitnami/nginx')"),
    strict: bool = Query(False, description="Tratta i warning come errori"),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Esegue helm lint su un chart da repository.
    Restituisce sempre HTTP 200 con has_errors/has_warnings nel body —
    non solleva eccezione su lint fallito (è un risultato, non un errore di sistema).
    """
    result = await manager.lint(chart_ref=chart_ref, strict=strict)
    return result


@router.post("/charts/lint-zip")
async def lint_chart_from_zip(
    file: UploadFile = File(..., description="ZIP con il chart Helm"),
    strict: bool = Query(False),
    values_json: Optional[str] = Query(None, description="Valori override come JSON string"),
    manager: HelmManager = Depends(get_helm_manager),
):
    """
    Esegue helm lint su un chart fornito come ZIP.
    Utile per validare prima di eseguire install_from_zip.
    """
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Il file deve essere un .zip")

    content = await file.read()
    values: dict | None = None
    if values_json:
        try: values = json.loads(values_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"values_json non valido: {exc}")

    result = await manager.lint(zip_bytes=content, values=values, strict=strict)
    return result