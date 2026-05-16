import asyncio
import urllib3
from functools import partial
from concurrent.futures import ThreadPoolExecutor
from app.infrastructure.database import SessionLocal, ClusterModel, ProfileModel
from app.infrastructure.k8s_factory import K8sClientFactory
from app.core.core_manager import CoreManager

ADMIN_PROFILE_NAMES = ["admin", "gateway-admin", "cluster-admin"]
SCAN_TIMEOUT = 5 
executor = ThreadPoolExecutor(max_workers=50)

async def scan_all_clusters() -> list[dict]:
    db = SessionLocal()
    try:
        clusters = db.query(ClusterModel).all()
        cluster_configs = []
        for cluster in clusters:
            profile = _find_best_profile(db, cluster.id)
            cluster_configs.append({
                "cluster_id":   cluster.id,
                "cluster_name": cluster.name,
                "host":         cluster.host,
                "ca_cert":      cluster.ca_cert,
                "k8s_token":    profile.k8s_token if profile else None,
                "profile_name": profile.name if profile else None,
            })
    finally:
        db.close()

    if not cluster_configs:
        return []

    # Esecuzione parallela
    tasks = [_scan_single_cluster(cfg) for cfg in cluster_configs]
    return await asyncio.gather(*tasks)

def _find_best_profile(db, cluster_id: str):
    for name in ADMIN_PROFILE_NAMES:
        p = db.query(ProfileModel).filter(ProfileModel.cluster_id == cluster_id.upper(), ProfileModel.name == name).first()
        if p: return p
    return db.query(ProfileModel).filter(ProfileModel.cluster_id == cluster_id.upper()).first()

async def _scan_single_cluster(cfg: dict) -> dict:
    base = {
        "cluster_id": cfg["cluster_id"],
        "cluster_name": cfg["cluster_name"],
        "host": cfg["host"],
        "profile_used": cfg["profile_name"],
        "status": "offline",
        "server_version": "N/A",
        "stats": {
            "cpu_total": 0, "pods_total": 0, "pods_running": 0, 
            "pods_failed": 0, "pods_pending": 0, "services_lb": 0, 
            "deployments_single_replica": 0, "namespaces_total": 0
        },
        "nodes": [],
        "namespaces": {"items": []},
        "error": None
    }

    if not cfg["k8s_token"]:
        base["error"] = "No admin token"
        return base

    try:
        loop = asyncio.get_running_loop()
        
        # 1. Init Client
        k8s_apis = await asyncio.wait_for(
            loop.run_in_executor(executor, partial(
                K8sClientFactory.get_apis,
                cluster_host=cfg["host"], k8s_token=cfg["k8s_token"],
                ca_cert=cfg["ca_cert"], cluster_id=cfg["cluster_id"]
            )), timeout=SCAN_TIMEOUT
        )
        manager = CoreManager(k8s_apis)

        # 2. Prepariamo le funzioni
        # NOTA: Assicurati che questi metodi esistano nel tuo CoreManager
        f_nodes = partial(manager.list_nodes, _request_timeout=SCAN_TIMEOUT)
        f_ns    = partial(manager.list_namespaces, _request_timeout=SCAN_TIMEOUT)
        f_pods  = partial(manager.list_pods, namespace=None, _request_timeout=SCAN_TIMEOUT)
        f_ver   = partial(manager.check_connectivity)
        f_quotas = partial(manager.list_resource_quotas, namespace=None, _request_timeout=SCAN_TIMEOUT)
        f_svcs   = partial(manager.list_services, namespace=None, _request_timeout=SCAN_TIMEOUT)
        f_depl   = partial(manager.list_deployments_fleet, namespace=None, _request_timeout=SCAN_TIMEOUT)

        # 3. Lanciamo i thread
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    loop.run_in_executor(executor, f_nodes),
                    loop.run_in_executor(executor, f_ns),
                    loop.run_in_executor(executor, f_pods),
                    loop.run_in_executor(executor, f_ver),
                    loop.run_in_executor(executor, f_quotas), 
                    loop.run_in_executor(executor, f_svcs),   
                    loop.run_in_executor(executor, f_depl),
                    return_exceptions=True 
                ), 
                timeout=SCAN_TIMEOUT + 2 
            )
        except asyncio.TimeoutError:
            base["error"] = "Cluster connection timed out during gather"
            return base

        # --- FIX SICUREZZA: Controllo che results sia una lista di 7 elementi ---
        if not isinstance(results, list) or len(results) != 7:
            # Se results è un'eccezione singola o incompleta, lo catturiamo qui
            base["error"] = f"Invalid scan results: {results}"
            return base

        # 4. Parsing Risultati (Unpacking sicuro)
        res_nodes, res_ns, res_pods, res_ver, res_quotas, res_svcs, res_depl = results

        # Pulizia dati (se è un'eccezione, usiamo valori di fallback sicuri)
        nodes = res_nodes if not isinstance(res_nodes, Exception) else []
        namespaces = res_ns if not isinstance(res_ns, Exception) else {"items": []}
        pods = res_pods if isinstance(res_pods, list) else []
        version = res_ver if isinstance(res_ver, dict) else {}
        quotas = res_quotas if isinstance(res_quotas, list) else []
        services = res_svcs if isinstance(res_svcs, list) else []
        deployments = res_depl if isinstance(res_depl, list) else []

        if not nodes and not namespaces.get("items"):
            base["error"] = "Unreachable or empty results"
            return base

        # --- LOGICA DI ELABORAZIONE ---
        ns_with_quota = {q.get("namespace") for q in quotas if isinstance(q, dict)}
        
        pod_running = sum(1 for p in pods if p.get("status") == "Running")
        pod_failed = sum(1 for p in pods if p.get("status") in ["Failed", "Error", "CrashLoopBackOff"])
        pod_pending = sum(1 for p in pods if p.get("status") == "Pending")
        
        services_lb = sum(1 for s in services if s.get("type") == "LoadBalancer")
        single_replicas = sum(1 for d in deployments if d.get("replicas") == 1)

        # Arricchiamo i namespace
        ns_items = namespaces.get("items", [])
        for ns in ns_items:
            ns["has_quota"] = ns["name"] in ns_with_quota

        # Calcolo CPU totale
        total_cpu = 0
        for n in nodes:
            try: total_cpu += int(n.get("cpu", 0))
            except: pass

        return {
            **base,
            "status": "online" if nodes and all(n.get("status") == "Ready" for n in nodes) else "degraded",
            "server_version": version.get("server_version", "N/A"),
            "stats": {
                "cpu_total": total_cpu,
                "pods_total": len(pods),
                "pods_running": pod_running,
                "pods_failed": pod_failed,
                "pods_pending": pod_pending,
                "services_lb": services_lb,
                "deployments_single_replica": single_replicas,
                "namespaces_total": len(ns_items)
            },
            "nodes": nodes,
            "namespaces": {"items": ns_items}
        }

    except Exception as e:
        base["error"] = f"Scanner internal error: {str(e)}"
        return base