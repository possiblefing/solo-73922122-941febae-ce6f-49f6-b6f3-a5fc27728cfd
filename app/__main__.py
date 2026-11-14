"""Entry point for ``python -m app`` / uvicorn.

``ENERGY_SEED_DEMO=1`` loads a small idempotent demo data set at startup.
"""

from __future__ import annotations

import os

from .main import create_app

app = create_app(seed_demo=os.environ.get("ENERGY_SEED_DEMO", "") == "1")
