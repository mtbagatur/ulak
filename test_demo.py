from copy import deepcopy

from fastapi.testclient import TestClient

from tools import demo_server as demo


def test_demo_approval_resolves_the_fake_session():
    original = deepcopy(demo.SESSIONS)
    try:
        client = TestClient(demo.app)
        response = client.post("/api/sessions/d1:p1/approve", json={"decision": "approve", "fingerprint": "demo-a"})
        assert response.status_code == 200 and response.json()["ok"]
        chat = client.get("/api/sessions/d1:p1/chat").json()
        assert chat["status"] == "done" and chat["prompt"] is None
        assert chat["receipt"]["decision"] == "approve" and chat["receipt"]["delivered"]
        assert client.get("/api/audit").json()["items"][0]["outcome"] == "sent"
    finally:
        demo.SESSIONS[:] = original
        demo.AUDIT.clear()


def test_demo_never_serves_a_stale_app():
    client = TestClient(demo.app)
    for path in ("/", "/static/app.js", "/static/i18n.js", "/sw.js"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
