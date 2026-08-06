"""Entry point: starts scheduler + FastAPI dashboard."""

from __future__ import annotations

import logging

import uvicorn

from src.config import load_pipeline_config
from src.db import store
from src.scheduler import shutdown_scheduler, start_scheduler
from src.web.app import _retry_failed_uploads, _scheduled_run, app

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    store.init_db()
    orphaned = store.fail_orphaned_runs()
    if orphaned:
        logger.warning(
            "Marked %s orphaned run(s) as failed after restart: %s",
            len(orphaned),
            orphaned,
        )

    config = load_pipeline_config()
    web_cfg = config.get("web", {})
    host = web_cfg.get("host", "127.0.0.1")
    port = int(web_cfg.get("port", 8081))

    start_scheduler(_scheduled_run, retry_uploads_callback=_retry_failed_uploads)
    logger.info("Scheduler started — daily 10:00 AM and 10:00 PM IST for every section")
    logger.info("Failed YouTube uploads will retry every 1 hour")
    logger.info("Dashboard: http://%s:%s", host, port)

    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        shutdown_scheduler()


if __name__ == "__main__":
    main()
