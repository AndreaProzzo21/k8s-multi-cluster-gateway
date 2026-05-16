from fastapi import Request
from fastapi.responses import JSONResponse # Importa questo
from starlette.middleware.base import BaseHTTPMiddleware
from collections import defaultdict
import time
import logging

logger = logging.getLogger("k8s_gateway")

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, calls_per_minute: int = 20):
        super().__init__(app)
        self.calls_per_minute = calls_per_minute
        self.request_history = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(("/docs", "/openapi.json", "/redoc")):
            return await call_next(request)

        client_ip = request.client.host
        now = time.time()
        
        self.request_history[client_ip] = [
            t for t in self.request_history[client_ip] if now - t < 60
        ]

        if len(self.request_history[client_ip]) >= self.calls_per_minute:
            logger.warning(f"🚫 Rate limit hit for IP: {client_ip}")
            # NON USARE RAISE. Ritorna direttamente la risposta.
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."}
            )

        self.request_history[client_ip].append(now)
        return await call_next(request)