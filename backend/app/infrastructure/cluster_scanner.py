import asyncio
import urllib3
from functools import partial
from concurrent.futures import ThreadPoolExecutor
from app.infrastructure.database import SessionLocal, ClusterModel, ProfileModel
from app.infrastructure.k8s_factory import K8sClientFactory
from app.core.core_manager import CoreManager

ADMIN_PROFILE_NAMES = ["admin", "gateway-admin", "cluster-admin"]
SCAN_TIMEOUT = 5 
executor = ThreadPoolExecutor(max_workers=20)

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
        "stats": {"cpu_total": 0, "pods_total": 0, "pods_running": 0, "pods_failed": 0, "namespaces_total": 0},
        "nodes": [],
        "namespaces": {"items": []},
        "error": None
    }

    if not cfg["k8s_token"]:
        base["error"] = "No admin token"
        return base

    try:
        loop = asyncio.get_running_loop()
        
        # 1. Init Client (Limitiamo l'attesa sulla creazione della factory)
        k8s_apis = await asyncio.wait_for(
            loop.run_in_executor(executor, partial(
                K8sClientFactory.get_apis,
                cluster_host=cfg["host"], k8s_token=cfg["k8s_token"],
                ca_cert=cfg["ca_cert"], cluster_id=cfg["cluster_id"]
            )), timeout=SCAN_TIMEOUT
        )
        manager = CoreManager(k8s_apis)

        # 2. Prepariamo le funzioni sincrone da lanciare nei thread
        # Usiamo partial per passare gli argomenti correttamente
        f_nodes = partial(manager.list_nodes, _request_timeout=SCAN_TIMEOUT)
        f_ns    = partial(manager.list_namespaces, _request_timeout=SCAN_TIMEOUT)
        f_pods  = partial(manager.list_pods, namespace=None, _request_timeout=SCAN_TIMEOUT)
        f_ver   = partial(manager.check_connectivity)

        # 3. Lanciamo i thread e usiamo wait_for per il timeout globale del cluster
        # Questo risolve l'errore "coroutine expected" perché wait_for ora aspetta i Future
        # dell'executor in modo corretto.
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    loop.run_in_executor(executor, f_nodes),
                    loop.run_in_executor(executor, f_ns),
                    loop.run_in_executor(executor, f_pods),
                    loop.run_in_executor(executor, f_ver),
                    return_exceptions=True # Cattura errori K8s senza crashare lo scanner
                ), 
                timeout=SCAN_TIMEOUT + 1 # Un secondo in più rispetto al timeout SDK
            )
        except asyncio.TimeoutError:
            base["error"] = "Cluster connection timed out"
            return base

        # 4. Parsing Risultati
        nodes, namespaces, pods, version = results

        # Verifichiamo se sono eccezioni invece di dati reali
        nodes = nodes if not isinstance(nodes, Exception) else None
        namespaces = namespaces if not isinstance(namespaces, Exception) else None
        pods = pods if not isinstance(pods, Exception) else []
        version = version if not isinstance(version, Exception) else {}

        if not nodes and not namespaces:
            base["error"] = "Unreachable (Check VPN or Endpoint)"
            return base

        # --- STATS CALC ---
        total_cpu = 0
        if nodes:
            for n in nodes:
                try: total_cpu += int(n.get("cpu", 0))
                except: pass
        
        pod_list = pods if isinstance(pods, list) else []
        pod_running = sum(1 for p in pod_list if p.get("status") == "Running")
        pod_failed = sum(1 for p in pod_list if p.get("status") in ["Failed", "Error", "CrashLoopBackOff"])
        ready_nodes = sum(1 for n in nodes if n.get("status") == "Ready") if nodes else 0
        
        return {
            **base,
            "status": "online" if nodes and ready_nodes == len(nodes) else "degraded",
            "server_version": version.get("server_version", "N/A") if version else "N/A",
            "stats": {
                "cpu_total": total_cpu,
                "pods_total": len(pod_list),
                "pods_running": pod_running,
                "pods_failed": pod_failed,
                "namespaces_total": len(namespaces["items"]) if namespaces else 0
            },
            "nodes": nodes or [],
            "namespaces": namespaces or {"items": []}
        }

    except Exception as e:
        base["error"] = f"Communication error: {str(e)}"
        return base