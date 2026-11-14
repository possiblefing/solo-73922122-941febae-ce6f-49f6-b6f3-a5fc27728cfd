"""End-to-end smoke test against a running Compose deployment.

Usage:
    python scripts/e2e_compose.py http://localhost:8000
"""

from __future__ import annotations

import json
import os
import sys

import httpx


def main(base_url: str, second_url: str | None = None) -> int:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        health = client.get("/health")
        assert health.status_code == 200, health.text
        print("health:", health.json())

        batch = {
            "readings": [
                {
                    "meter_id": "E2E-M1",
                    "source_record_id": "e2e-00",
                    "read_at": "2026-02-01T00:00:00Z",
                    "reading": 1000,
                },
                {
                    "meter_id": "E2E-M1",
                    "source_record_id": "e2e-02",
                    "read_at": "2026-02-01T02:00:00Z",
                    "reading": 1300,
                },
                {
                    "meter_id": "E2E-M1",
                    "source_record_id": "e2e-03",
                    "read_at": "2026-02-01T03:00:00Z",
                    "reading": 20,
                    "meter_replaced": True,
                },
                {
                    "meter_id": "E2E-M1",
                    "source_record_id": "e2e-04",
                    "read_at": "2026-02-01T04:00:00Z",
                    "reading": 75,
                },
            ]
        }
        # First submit with a gap (02:00 missing), then backfill it.
        initial = [r for r in batch["readings"] if r["source_record_id"] != "e2e-02"]
        backfill = [r for r in batch["readings"] if r["source_record_id"] == "e2e-02"]
        assert client.post("/api/v1/readings", json={"readings": initial}).status_code == 200
        assert client.post("/api/v1/readings", json={"readings": backfill}).status_code == 200

        retry = client.post("/api/v1/readings", json=batch)
        assert retry.status_code == 200 and retry.json()["changed"] is False
        print("idempotent retry:", retry.json()["counts"])

        conflict = client.post(
            "/api/v1/readings",
            json={
                "readings": [
                    {
                        "meter_id": "E2E-M1",
                        "source_record_id": "e2e-00",
                        "read_at": "2026-02-01T00:00:00Z",
                        "reading": 1001,
                    }
                ]
            },
        )
        assert conflict.status_code == 409, conflict.text
        print("conflict rejected:", conflict.json()["conflicts"][0]["type"])

        undo = client.post(
            "/api/v1/readings/undo", json={"source_record_ids": ["e2e-02"]}
        )
        assert undo.status_code == 200
        print("undo:", undo.json()["counts"])

        state = client.get("/api/v1/meters/E2E-M1").json()
        print("final state:")
        print(json.dumps(state, ensure_ascii=False, indent=2))
        # e2e-02 is gone: 1000 -> 20 is a meter-replacement break (no
        # usage), 20 -> 75 is 55.
        assert state["usage_total"] == 55
        assert state["breaks"][0]["break_reason"] == "meter_replaced"

        listing = client.get("/api/v1/meters").json()
        assert any(m["meter_id"] == "E2E-M1" for m in listing)

    # Optional: prove both processes serve the same data.
    if second_url:
        with httpx.Client(base_url=second_url, timeout=30.0) as other:
            other_state = other.get("/api/v1/meters/E2E-M1").json()
        assert other_state["commit_seq"] == state["commit_seq"]
        assert other_state["intervals"] == state["intervals"]
        print("second process sees the same snapshot:", other_state["commit_seq"])

    # /docs must be usable.
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        docs = client.get("/docs")
        assert docs.status_code == 200
        spec = client.get("/openapi.json").json()
        paths = spec["paths"]
        assert "/api/v1/readings" in paths
        assert "/api/v1/readings/undo" in paths
        assert "/api/v1/meters/{meter_id}" in paths
    print("openapi paths ok")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "BASE_URL", "http://localhost:8000"
    )
    second = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SECOND_URL")
    raise SystemExit(main(base, second))
