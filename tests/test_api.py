from fastapi.testclient import TestClient

from src.api.routes import project as project_module
from src.core.config import settings
from src.main import app

settings.ENABLE_DEV_AUTH = True
client = TestClient(app)


class StubGraph:
    async def ainvoke(self, state, config=None) -> dict:
        return {
            "architecture": "arch",
            "api_spec": "api",
            "security_findings": "APPROVED - ok",
            "revision_count": 1,
        }


class StubOptimizer:
    async def ainvoke(self, state, config=None) -> dict:
        return {
            "task": state["task"],
            "winning_prompt": "winner persona",
            "probe_results": [
                {
                    "prompt": "strict persona",
                    "revisions": 3,
                    "approved": False,
                    "architecture": "arch v1",
                    "api_spec": "api v1",
                    "findings": "CHANGES REQUESTED - raw cards",
                },
                {
                    "prompt": "winner persona",
                    "revisions": 1,
                    "approved": True,
                    "architecture": "arch v2",
                    "api_spec": "api v2",
                    "findings": "APPROVED - ok",
                },
            ],
        }


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_design_requires_auth():
    response = client.post(
        "/project/design", json={"requirements": "billing api", "session_id": "s1"}
    )
    assert response.status_code == 401

    response = client.post(
        "/project/design",
        json={"requirements": "billing api", "session_id": "s1"},
        headers={"Authorization": "Bearer garbage"},
    )
    assert response.status_code == 401


def test_design_endpoint_with_token(monkeypatch):
    monkeypatch.setattr(project_module, "get_multi_agent_app", lambda: StubGraph())

    token = client.post("/auth/token", json={"username": "keke"}).json()["access_token"]
    response = client.post(
        "/project/design",
        json={"requirements": "billing api", "session_id": "s1"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["approved"] is True
    assert body["total_revisions"] == 1


def test_optimize_requires_auth():
    response = client.post("/project/optimize", json={"requirements": "billing api"})
    assert response.status_code == 401


def test_optimize_endpoint_with_token(monkeypatch):
    monkeypatch.setattr(project_module, "get_harness_optimizer", lambda: StubOptimizer())

    token = client.post("/auth/token", json={"username": "keke"}).json()["access_token"]
    response = client.post(
        "/project/optimize",
        json={"requirements": "billing api"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["winning_prompt"] == "winner persona"
    assert body["approved"] is True
    assert body["final_architecture"] == "arch v2"
    assert body["total_revisions"] == 1
    assert len(body["probes"]) == 2


def test_cors_preflight():
    response = client.options(
        "/project/design",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_session_id_with_colon_is_rejected():
    token = client.post("/auth/token", json={"username": "keke"}).json()["access_token"]
    response = client.post(
        "/project/design",
        json={"requirements": "billing api", "session_id": "x:y"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_oversized_requirements_are_rejected():
    token = client.post("/auth/token", json={"username": "keke"}).json()["access_token"]
    response = client.post(
        "/project/design",
        json={
            "requirements": "x" * (project_module.MAX_REQUIREMENTS_CHARS + 1),
            "session_id": "s1",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_dev_auth_endpoint_hidden_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DEV_AUTH", False)
    response = client.post("/auth/token", json={"username": "keke"})
    assert response.status_code == 404


def test_concurrent_optimize_for_same_user_is_rejected(monkeypatch):
    from contextlib import asynccontextmanager

    from src.db.locks import RunInProgress

    @asynccontextmanager
    async def busy(scope, user_id):
        raise RunInProgress(f"{scope}:{user_id}")
        yield

    monkeypatch.setattr(project_module, "exclusive_user_run", busy)
    monkeypatch.setattr(project_module, "get_harness_optimizer", lambda: StubOptimizer())

    token = client.post("/auth/token", json={"username": "keke"}).json()["access_token"]
    response = client.post(
        "/project/optimize",
        json={"requirements": "billing api"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 429


def test_design_times_out_with_pointer_to_stream(monkeypatch):
    import asyncio

    class SlowGraph:
        async def ainvoke(self, state, config=None) -> dict:
            await asyncio.sleep(5)
            return {}

    monkeypatch.setattr(project_module, "get_multi_agent_app", lambda: SlowGraph())
    monkeypatch.setattr(settings, "DESIGN_TIMEOUT_SECONDS", 0.05)

    token = client.post("/auth/token", json={"username": "keke"}).json()["access_token"]
    response = client.post(
        "/project/design",
        json={"requirements": "billing api", "session_id": "s1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 504
    assert "/project/design/stream" in response.json()["detail"]


def test_optimize_reports_all_candidates_failed_as_502(monkeypatch):
    from src.harness.optimizer import AllCandidatesFailed

    class FailingOptimizer:
        async def ainvoke(self, state, config=None):
            raise AllCandidatesFailed(
                [{"failure_reason": "timeout"}, {"failure_reason": "provider_error"}]
            )

    monkeypatch.setattr(project_module, "get_harness_optimizer", lambda: FailingOptimizer())
    token = client.post("/auth/token", json={"username": "failing-optimizer"}).json()["access_token"]
    response = client.post(
        "/project/optimize",
        json={"requirements": "billing api"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "timeout" in detail and "provider_error" in detail


def test_hosted_optimizer_applies_the_harness_budget(monkeypatch):
    from src.harness import optimizer as optimizer_module

    monkeypatch.setattr(optimizer_module.settings, "HARNESS_REASONING_EFFORT", "none")
    monkeypatch.setattr(optimizer_module.settings, "MAX_REVISIONS", 5)
    monkeypatch.setattr(optimizer_module.settings, "HARNESS_MAX_REVISIONS", 2)
    monkeypatch.setattr(
        optimizer_module, "get_llm", lambda **kwargs: ("llm", kwargs.get("reasoning_effort"))
    )
    captured = {}
    monkeypatch.setattr(
        optimizer_module, "build_harness_optimizer", lambda **kwargs: captured.update(kwargs)
    )
    optimizer_module.get_harness_optimizer.cache_clear()
    try:
        optimizer_module.get_harness_optimizer()
    finally:
        optimizer_module.get_harness_optimizer.cache_clear()
    assert captured["llm"] == ("llm", "none")
    assert captured["agent_llm_factory"]() == ("llm", "none")
    assert captured["judge_llm"] == ("llm", "none")
    assert captured["max_revisions"] == 2


def test_optimize_over_the_call_budget_is_refused_before_any_call(monkeypatch):
    def must_not_run():
        raise AssertionError("the optimizer must not start")

    monkeypatch.setattr(project_module, "get_harness_optimizer", must_not_run)
    monkeypatch.setattr(settings, "HARNESS_CANDIDATES", 3)
    monkeypatch.setattr(settings, "HARNESS_MAX_REVISIONS", 2)
    monkeypatch.setattr(settings, "MAX_REVISIONS", 5)
    monkeypatch.setattr(settings, "HARNESS_MAX_MODEL_CALLS", 90)
    token = client.post("/auth/token", json={"username": "budget"}).json()["access_token"]
    response = client.post(
        "/project/optimize",
        json={"requirements": "billing api"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # 3 candidates x 2 attempts x (3 rounds x (1 design + 3 audit) + 3 judge) + 1 propose
    assert response.status_code == 403
    assert "91 model calls" in response.json()["detail"]


def test_optimize_at_exactly_the_call_budget_is_allowed(monkeypatch):
    monkeypatch.setattr(project_module, "get_harness_optimizer", lambda: StubOptimizer())
    monkeypatch.setattr(settings, "HARNESS_CANDIDATES", 3)
    monkeypatch.setattr(settings, "HARNESS_MAX_REVISIONS", 2)
    monkeypatch.setattr(settings, "MAX_REVISIONS", 5)
    monkeypatch.setattr(settings, "HARNESS_MAX_MODEL_CALLS", 91)
    token = client.post("/auth/token", json={"username": "budget-edge"}).json()["access_token"]
    response = client.post(
        "/project/optimize",
        json={"requirements": "billing api"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
