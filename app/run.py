"""Uvicorn entry point used by Docker Compose.

One process per container.  ``docker-compose.yml`` starts two containers that
share the same database volume, which is exactly the multi-process scenario
the storage layer is designed for.
"""

from __future__ import annotations

import os

import uvicorn

from .main import create_app

app = create_app(seed_demo=os.environ.get("ENERGY_SEED_DEMO", "") == "1")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
