"""Background worker for retryable / slow work (CRM sync, transcripts, usage)."""

import asyncio
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("synas.worker")


async def run_worker() -> None:
    logger.info("Synas worker started (queue consumer stub)")
    while True:
        # Placeholder loop — wire Redis/Dramatiq/Celery consumers here.
        logger.debug("worker heartbeat")
        await asyncio.sleep(30)


def main() -> None:
    logger.info("Booting worker")
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("Worker stopped")
        time.sleep(0.1)


if __name__ == "__main__":
    main()
