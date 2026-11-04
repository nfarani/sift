"""Demo mode: no real address, display name or subject reaches the browser, and actions still work."""
import json

from .test_api import client, wait  # noqa: F401  (fixture)


def _real_values(state):
    with state.db.connect() as c:
        addrs = {r[0] for r in c.execute("SELECT DISTINCT from_addr FROM messages")}
        subjects = {r[0] for r in c.execute("SELECT DISTINCT subject FROM messages WHERE length(subject) >= 12")}
        names = {r[0] for r in c.execute("SELECT DISTINCT display_name FROM sender_groups WHERE display_name LIKE '% %'")}
    return addrs, subjects, names


def _all_gets(c):
    out = []
    for path in ("/api/status", "/api/dashboard", "/api/scan", "/api/recommendations", "/api/ai",
                 "/api/protection-rules", "/api/runs"):
        r = c.get(path)
        assert r.status_code == 200, path
        out.append(r.text)
    return "\n".join(out)


def test_demo_mode_masks_every_response(client):  # noqa: F811
    c, state = client
    assert c.post("/api/scan").status_code == 200
    wait(c, "/api/scan")
    assert c.post("/api/demo", json={"enabled": True}).json()["demo_mode"] is True
    body = _all_gets(c)
    addrs, subjects, names = _real_values(state)
    assert addrs and subjects
    assert not [a for a in addrs if a in body], "real address leaked"
    assert not [s for s in subjects if s in body], "real subject leaked"
    assert not [n for n in names if n in body], "real person name leaked"
    assert state.provider.account_email() not in body
    assert "***" in body and "Sender #" in body and "[hidden in demo mode]" in body
    assert json.loads(c.get("/api/status").text)["demo_mode"] is True


def test_masked_sender_still_works_in_actions(client):  # noqa: F811
    c, state = client
    c.post("/api/scan")
    wait(c, "/api/scan")
    c.post("/api/demo", json={"enabled": True})
    rec = next(r for r in c.get("/api/recommendations").json()["items"] if r["action"] == "archive")
    masked = rec["senders"][0]["sender"]
    assert "***" in masked
    p = c.post("/api/plans", json={"items": [{"rec_id": rec["id"], "excluded_senders": [masked]}]}).json()
    with state.db.connect() as x:
        real = json.loads(x.execute("SELECT spec FROM plans WHERE id=?", (p["id"],)).fetchone()[0])[0]["excluded_senders"]
    assert real and "***" not in real[0] and "@" in real[0]  # the plan holds the real address


def test_demo_off_by_default_shows_real_data(client):  # noqa: F811
    c, state = client
    c.post("/api/scan")
    wait(c, "/api/scan")
    assert c.get("/api/status").json()["demo_mode"] is False
    addrs, _, _ = _real_values(state)
    assert any(a in c.get("/api/recommendations").text for a in addrs)


def test_masked_sender_survives_restart_and_unknown_masks_are_refused(client):  # noqa: F811
    from sift.api import create_app
    from fastapi.testclient import TestClient
    c, state = client
    c.post("/api/scan")
    wait(c, "/api/scan")
    c.post("/api/demo", json={"enabled": True})
    rec = next(r for r in c.get("/api/recommendations").json()["items"] if r["action"] == "archive")
    masked = rec["senders"][0]["sender"]
    c2 = TestClient(create_app(state), headers={"X-Sift": "1"})  # fresh masker, as after a restart
    p = c2.post("/api/plans", json={"items": [{"rec_id": rec["id"], "excluded_senders": [masked]}]})
    assert p.status_code == 200
    bogus = "z***deadbeef@nowhere.example"
    r = c2.post("/api/plans", json={"items": [{"rec_id": rec["id"], "excluded_senders": [bogus]}]})
    assert r.status_code == 409 and "masked" in r.json()["detail"]


def test_masked_address_refused_when_demo_is_off(client):  # noqa: F811
    """Audit BLOCKING 1: a stale demo-mode page can't save a masked string as an exclusion or rule."""
    c, state = client
    c.post("/api/scan")
    wait(c, "/api/scan")
    c.post("/api/demo", json={"enabled": True})
    rec = next(r for r in c.get("/api/recommendations").json()["items"] if r["action"] == "archive")
    masked = rec["senders"][0]["sender"]
    c.post("/api/demo", json={"enabled": False})
    r = c.post("/api/plans", json={"items": [{"rec_id": rec["id"], "excluded_senders": [masked]}]})
    assert r.status_code == 409
    r = c.post("/api/protection-rules", json={"kind": "sender", "value": masked})
    assert r.status_code == 409


def test_list_ids_are_masked(client):  # noqa: F811
    """Audit FAIL 2."""
    c, state = client
    c.post("/api/scan")
    wait(c, "/api/scan")
    with state.db.connect() as x:
        lids = {r[0] for r in x.execute("SELECT DISTINCT list_id FROM sender_groups WHERE list_id LIKE '%@%' "
                                        "OR list_id GLOB '*[0-9][0-9][0-9][0-9][0-9][0-9]*'")}
        x.execute("UPDATE sender_groups SET list_id='<news.12345678.person@mail.example.com>' WHERE rowid=1")
    c.post("/api/demo", json={"enabled": True})
    body = c.get("/api/recommendations").text
    assert "person@mail.example.com" not in body and "12345678" not in body
    assert not [l for l in lids if l and l in body]


def test_colliding_masks_are_never_unmasked():
    """Audit FAIL 3: if two addresses ever share a mask, the mask maps to neither."""
    from sift.demo import Masker, UnknownMasked
    import pytest
    m = Masker()
    a = m.address("ann@x.example")
    m._reverse[a + "x"] = "unused"
    m._reverse[a] = "ann@x.example"
    m._ambiguous.add(a)  # simulate a collision
    with pytest.raises(UnknownMasked):
        m.unmask({"excluded_senders": [a]})
