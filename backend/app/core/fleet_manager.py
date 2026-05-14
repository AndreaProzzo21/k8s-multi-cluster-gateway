import asyncio
import logging
from app.infrastructure.cluster_scanner import scan_all_clusters

logger = logging.getLogger("k8s_gateway")

# app/core/fleet_manager.py

class FleetManager:
    _cache = []
    _is_running = False

    @classmethod
    async def start_observer(cls, interval_seconds: int = 30):
        if cls._is_running: return
        cls._is_running = True
        logger.info(f"Fleet Observer avviato (intervallo: {interval_seconds}s)")
        
        while cls._is_running:
            try:
                # Eseguiamo lo scan
                new_data = await scan_all_clusters()
                if new_data:
                    cls._cache = new_data
                logger.info("Fleet cache aggiornata.")
            except Exception as e:
                logger.error(f"Errore Observer: {e}")
            
            await asyncio.sleep(interval_seconds)

    @classmethod
    def get_cached_status(cls):
        # Se la cache è ancora vuota (magari al primissimo avvio),
        # potresti voler restituire un messaggio o far aspettare l'utente,
        # ma con create_task solitamente è pronta in pochi secondi.
        return cls._cache

    @classmethod
    async def refresh(cls):
        """Esegue la scansione e aggiorna la cache. Ritorna i nuovi dati."""
        new_data = await scan_all_clusters()
        if new_data:
            cls._cache = new_data
            logger.info("Fleet cache aggiornata con successo.")
        return cls._cache

