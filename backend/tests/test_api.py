"""End-to-end through the HTTP API, exactly as the UI drives it, with background jobs."""
import time

import pytest
from fastapi.testclient import TestClient

from sift.api import build_state, create_app
from sift.config import Settings


@pytest.fixture
def client(tmp_path, provider, retry):
    s = Settings()
    s.data_dir = tmp_path
    s.first_run_cap = 50
    s.extra_allowed_hosts = ("testserver",)
    state = build_state(s, provider=provider, retry=retry)
    state.scanner.page_size, state.scanner.meta_batch = 200, 50
    return TestClient(create_app(state), headers={"X-Sift": "1"}), state


def wait(c, path, done=lambda d: d["status"] != "running", timeout=30):
    t = time.time()
    while time.time() - t < timeout:
        d = c.get(path).json()
        if done(d):
            return d
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting on {path}")


def test_full_flow(client):
    c, state = client
    st = c.get("/api/status").json()
    assert st["synthetic"] is True
    assert st["classifier"]["uses_ai"] is False and "no AI" in st["classifier"]["label"]
    assert "token" not in str(st).lower()

    assert c.post("/api/scan").status_code == 200
    scan = wait(c, "/api/scan")
    assert scan["status"] == "completed" and scan["messages_scanned"] > 0

    dash = c.get("/api/dashboard").json()
    assert dash["inbox"] > 0 and dash["protected"] > 0 and dash["recommended"] > 0

    recs = c.get("/api/recommendations").json()["items"]
    promo = next(r for r in recs if r["id"] == "promotions")
    assert promo["count"] > 0 and promo["examples"] and promo["exceptions"]
    top = promo["senders"][0]["sender"]
    prev = c.post("/api/recommendations/promotions/preview",
                  json={"older_than_days": 30, "excluded_senders": [top]}).json()
    assert prev["count"] < promo["count"]

    plan = c.post("/api/plans", json={"items": [{"rec_id": "promotions", "older_than_days": 30,
                                                  "excluded_senders": [top]}]}).json()
    assert plan["status"] == "draft" and plan["total"] == prev["count"]
    assert c.post(f"/api/plans/{plan['id']}/execute").status_code == 400  # not approved yet
    assert c.post(f"/api/plans/{plan['id']}/approve", json={"confirm_hash": "nope"}).status_code == 400
    assert c.post(f"/api/plans/{plan['id']}/approve", json={"confirm_hash": plan["ids_hash"]}).json()["status"] == "approved"

    run = c.post(f"/api/plans/{plan['id']}/execute").json()
    run = wait(c, f"/api/runs/{run['id']}")
    assert run["status"] == "completed" and run["cap"] == 50 and run["confirmed"] == 50
    assert run["inbox_now"] == run["plan"]["inbox_before"] - 50

    assert c.post("/api/cleanup/unlock").status_code == 400  # not before a verified restore
    rest = c.post(f"/api/runs/{run['id']}/restore").json()
    rest = wait(c, f"/api/runs/{rest['id']}")
    assert rest["status"] == "completed" and rest["confirmed"] == 50
    assert c.post("/api/cleanup/unlock").json()["full_runs_unlocked"] is True

    pid = plan["id"]
    run2 = c.post(f"/api/plans/{pid}/execute").json()
    run2 = wait(c, f"/api/runs/{run2['id']}")
    assert run2["status"] == "completed" and run2["plan_remaining"] == 0
    audit = c.get(f"/api/runs/{run2['id']}/audit").json()
    assert audit and all(a["status"] in ("attempted", "confirmed", "skipped", "failed") for a in audit)


def test_scan_cancel_and_resume_via_api(client):
    c, state = client
    state.provider.latency = 0.01
    c.post("/api/scan")
    wait(c, "/api/scan", done=lambda d: d["messages_scanned"] > 0)
    assert c.post("/api/scan/cancel").status_code == 200
    d = wait(c, "/api/scan")
    assert d["status"] == "cancelled" and d["resumable"]
    state.provider.latency = 0
    assert c.post("/api/scan/resume").status_code == 200
    assert wait(c, "/api/scan")["status"] == "completed"


def test_protection_rule_removes_sender_from_recommendation(client):
    c, _ = client
    c.post("/api/scan")
    wait(c, "/api/scan")
    promo = next(r for r in c.get("/api/recommendations").json()["items"] if r["id"] == "promotions")
    top = promo["senders"][0]
    c.post("/api/protection-rules", json={"kind": "sender", "value": top["sender"]})
    promo2 = next(r for r in c.get("/api/recommendations").json()["items"] if r["id"] == "promotions")
    assert promo2["count"] == promo["count"] - top["eligible"]


def test_concurrent_jobs_rejected(client):
    c, state = client
    state.provider.latency = 0.01
    c.post("/api/scan")
    assert c.post("/api/scan").status_code == 409
    c.post("/api/scan/cancel")
    wait(c, "/api/scan")


def test_cross_site_and_rebinding_requests_rejected(client):
    c, _ = client
    raw = TestClient(c.app)  # no X-Sift header
    assert raw.post("/api/scan").status_code == 403
    assert c.post("/api/scan", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/status", headers={"Host": "attacker.example"}).status_code == 403
    assert c.post("/api/scan", headers={"Host": "attacker.example"}).status_code == 403
    assert raw.get("/api/status").status_code == 200  # reads from the local UI still work
    c.post("/api/scan/cancel")


def test_execute_refused_while_scan_incomplete(client):
    c, state = client
    c.post("/api/scan")
    wait(c, "/api/scan")
    plan = c.post("/api/plans", json={"items": [{"rec_id": "promotions"}]}).json()
    c.post(f"/api/plans/{plan['id']}/approve", json={"confirm_hash": plan["ids_hash"]})
    state.provider.latency = 0.01
    c.post("/api/scan")  # a fresh scan is now running/partial
    c.post("/api/scan/cancel")
    wait(c, "/api/scan")
    r = c.post(f"/api/plans/{plan['id']}/execute")
    assert r.status_code == 400 and "scan" in r.json()["detail"]
