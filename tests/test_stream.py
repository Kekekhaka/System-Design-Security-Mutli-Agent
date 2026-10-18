from langgraph.checkpoint.memory import InMemorySaver
from fastapi.testclient import TestClient

from src.agents.multi_agent_graph import build_multi_agent
from src.api.routes import project as project_module
from src.core.config import settings
from src.main import app
from tests.fakes import ScriptedLLM

settings.ENABLE_DEV_AUTH = True
client = TestClient(app)


DESIGN = (
    "===ARCHITECTURE===\n"
    "Architecture: card data goes to the payment provider; only tokens reach the backend.\n"
    "===API SPEC===\n"
    "POST /pay {payment_token, amount}"
)

SCOPE = "Scope: card flow, gateway logging."
DRAFT = "Draft findings: tokens only; scoping present."
APPROVE = "APPROVED\nOnly tokens flow through the backend."


def test_design_stream_requires_auth():
    response = client.post(
        "/project/design/stream",
        json={"requirements": "billing api", "session_id": "s1"},
    )
    assert response.status_code == 401


def test_design_stream_emits_nodes_and_final(monkeypatch):
    llm = ScriptedLLM([DESIGN, SCOPE, DRAFT, APPROVE])
    graph = build_multi_agent(llm=llm, checkpointer=InMemorySaver())
    monkeypatch.setattr(project_module, "get_multi_agent_app", lambda: graph)

    token = client.post("/auth/token", json={"username": "keke"}).json()["access_token"]
    response = client.post(
        "/project/design/stream",
        json={"requirements": "billing api", "session_id": "s-stream"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "event: node" in body
    assert '"node": "designer"' in body
    assert "event: final" in body
    assert '"approved": true' in body
