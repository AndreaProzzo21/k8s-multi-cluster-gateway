import time
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import asyncio

# Import interni
from app.core.exceptions import K8sBaseException
from app.api.routes.k8s_routes import router as k8s_router
from app.api.routes.helm_routes import router as helm_router
from app.api.auth.auth_route import auth_router
from app.api.routes.admin_routes import admin_router
from app.infrastructure.database import init_db
from app.core.fleet_manager import FleetManager

# Configurazione Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("k8s_gateway")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestisce l'avvio e lo spegnimento dell'applicazione.
    Sostituisce i vecchi eventi startup/shutdown.
    """
    logger.info("Avvio K8S Digital Twin Gateway...")
    
    # Inizializzazione DB (es. creazione tabelle se non esistono)
    init_db()

    # --- AVVIO OBSERVER IN BACKGROUND ---
    # Usiamo create_task per non bloccare l'avvio del server
    asyncio.create_task(FleetManager.start_observer(interval_seconds=30))
    yield
    logger.info("Spegnimento Gateway...")

def create_app() -> FastAPI:
    app = FastAPI(
        title="K8S Digital Twin Gateway",
        description="API Proxy Stateless per la gestione multi-cluster via JWT",
        version="2.0.0",
        lifespan=lifespan
    )

    # --- MIDDLEWARE ---

    # 1. CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], # In produzione, specifica i domini reali
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 2. Timing Middleware (Logging & Performance)
    @app.middleware("http")
    async def add_process_time_header(request: Request, call_next):
        start_time = time.perf_counter()
        
        response = await call_next(request)
        
        process_time = time.perf_counter() - start_time
        response.headers["X-Process-Time"] = f"{process_time:.4f}s"
        
        logger.info(
            f"Method: {request.method} | Path: {request.url.path} | "
            f"Status: {response.status_code} | Duration: {process_time:.4f}s"
        )
        return response

    # --- EXCEPTION HANDLERS ---

    @app.exception_handler(K8sBaseException)
    async def k8s_exception_handler(request: Request, exc: K8sBaseException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": "K8S_GATEWAY_ERROR", 
                "message": exc.message,
                "status_code": exc.status_code
            },
        )

    # --- REGISTRAZIONE ROTTE ---
    
    # Rotte Pubbliche
    app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
    
    # Rotte Protette
    app.include_router(k8s_router, prefix="/api/v1", tags=["Kubernetes Operations"])
    app.include_router(helm_router, prefix="/api/v1/helm", tags=["Helm Management"])
    
    # Rotte Administrative
    app.include_router(admin_router, prefix="/api/v1/admin", tags=["Admin Operations"])

    return app