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
    finally:
        demo.SESSIONS[:] = original
