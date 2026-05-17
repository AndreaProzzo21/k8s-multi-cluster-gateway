import time
import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Import Middleware personalizzati
from app.api.rate_limiter import RateLimitMiddleware

# Import Interni
from app.core.exceptions import K8sBaseException
from app.api.routes.k8s_routes import router as k8s_router
from app.api.routes.helm_routes import router as helm_router
from app.api.auth.auth_route import auth_router
from app.api.routes.admin_routes import admin_router
from app.api.routes.audit_routes import audit_router
from app.infrastructure.database import init_db
from app.core.fleet_manager import FleetManager

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("k8s_gateway")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """ Lifecycle manager per startup e shutdown del gateway """
    logger.info("🚀 Avvio K8S Cloud Gateway...")
    init_db()
    
    # Background Task: Observer per la salute dei cluster
    asyncio.create_task(FleetManager.start_observer(interval_seconds=30))
    
    yield
    logger.info("🛑 Spegnimento Gateway...")

def create_app() -> FastAPI:
    app = FastAPI(
        title="K8S Cloud Gateway",
        description="Integrated Framework for Multi-Cluster Kubernetes Governance",
        version="1.0.0",
        lifespan=lifespan
    )

    # --- MIDDLEWARE CHAIN ---

    # 1. Protezione Rate Limit (IP-based)
    app.add_middleware(RateLimitMiddleware, calls_per_minute=40)

    # 2. Configurazione CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # # 3. Performance Logging (Timing Middleware)
    # @app.middleware("http")
    # async def add_process_time_header(request: Request, call_next):
    #     start_time = time.perf_counter()
    #     response = await call_next(request)
    #     process_time = time.perf_counter() - start_time
    #     response.headers["X-Process-Time"] = f"{process_time:.4f}s"
        
    #     logger.info(
    #         f"REQ: {request.method} {request.url.path} | "
    #         f"RES: {response.status_code} | TIME: {process_time:.4f}s"
    #     )
    #     return response

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
    
    @app.get("/health", tags=["System"])
    async def health_check():
        """
        Verifica lo stato di salute del Gateway.
        Utile per Kubernetes Liveness/Readiness Probes.
        """
        return {
            "status": "healthy",
            "timestamp": time.time(),
            "version": "1.0.0"
        }

    # --- ROUTING REGISTRATION ---
    
    # Prefix unificato /api/v1 per coerenza con il Reverse Proxy
    app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authentication"])
    app.include_router(k8s_router, prefix="/api/v1", tags=["Kubernetes Operations"])
    app.include_router(helm_router, prefix="/api/v1/helm", tags=["Helm Management"])
    app.include_router(admin_router, prefix="/api/v1/admin", tags=["Admin Operations"])
    app.include_router(audit_router, prefix="/api/v1/admin/audit", tags=["Audit Operations"])

    return app

app = create_app()