"""Deployment safety: service tokens, the smoke test, and the container files."""

import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

import jwt
import pytest
import yaml
from fastapi.testclient import TestClient

from src.core.config import settings
from src.core.security import create_access_token, jwt_secret
from src.main import app

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mint = load_script("mint_service_token")
smoke = load_script("smoke_test")
client = TestClient(app)
INVALID_BODY = {"requirements": "smoke", "session_id": "not:valid"}


# --- service token -----------------------------------------------------------

def test_default_token_lifetime_is_unchanged():
    payload = jwt.decode(create_access_token("alice"), jwt_secret(), algorithms=["HS256"])
    assert payload["exp"] - payload["iat"] == settings.JWT_EXPIRE_MINUTES * 60


def test_user_tokens_can_be_revoked_too(monkeypatch):
    token = create_access_token("alice")
    jti = jwt.decode(token, jwt_secret(), algorithms=["HS256"])["jti"]
    monkeypatch.setattr(settings, "REVOKED_TOKEN_IDS", jti)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/project/design", json=INVALID_BODY, headers=headers).status_code == 401


def mint_token(capsys, *argv) -> tuple[dict, str]:
    mint.main(["--subject", "openclaw", *argv])
    captured = capsys.readouterr()
    token = captured.out.strip()
    return jwt.decode(token, jwt_secret(), algorithms=["HS256"]), token


def test_service_token_is_namespaced_and_has_an_id(capsys):
    payload, _ = mint_token(capsys, "--days", "7")
    assert payload["sub"] == "svc:openclaw"
    assert len(payload["jti"]) == 16
    assert payload["exp"] - payload["iat"] == 7 * 24 * 3600
    assert payload["exp"] > datetime.now(timezone.utc).timestamp()


def test_each_service_token_gets_a_new_id(capsys):
    first, _ = mint_token(capsys)
    second, _ = mint_token(capsys)
    assert first["jti"] != second["jti"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--subject", "open:claw"],
        ["--subject", "svc:openclaw"],
        ["--subject", ""],
        ["--subject", "x" * 65],
        ["--subject", "openclaw", "--days", "0"],
        ["--subject", "openclaw", "--days", "31"],
    ],
)
def test_mint_rejects_bad_input(argv):
    with pytest.raises(SystemExit):
        mint.main(argv)


def test_mint_out_file_keeps_the_token_off_the_screen(tmp_path, capsys):
    out = tmp_path / "openclaw.token"
    mint.main(["--subject", "openclaw", "--out", str(out)])
    captured = capsys.readouterr()
    token = out.read_text().strip()
    assert captured.out == "" and token not in captured.err
    assert jwt.decode(token, jwt_secret(), algorithms=["HS256"])["sub"] == "svc:openclaw"
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600


def test_mint_out_file_never_overwrites(tmp_path):
    out = tmp_path / "openclaw.token"
    out.write_text("existing")
    with pytest.raises(SystemExit):
        mint.main(["--subject", "openclaw", "--out", str(out)])
    assert out.read_text() == "existing"


def test_a_revoked_token_is_refused(capsys, monkeypatch):
    payload, token = mint_token(capsys)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/project/design", json=INVALID_BODY, headers=headers).status_code == 422
    monkeypatch.setattr(settings, "REVOKED_TOKEN_IDS", f"other, {payload['jti']}")
    assert client.post("/project/design", json=INVALID_BODY, headers=headers).status_code == 401
    other, other_token = mint_token(capsys)
    other_headers = {"Authorization": f"Bearer {other_token}"}
    assert client.post("/project/design", json=INVALID_BODY, headers=other_headers).status_code == 422


def test_users_cannot_take_a_service_subject():
    response = client.post("/auth/token", json={"username": "svc:openclaw"})
    assert response.status_code == 400


def test_mint_refuses_a_placeholder_secret(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET", "change-me-to-a-long-random-string")
    with pytest.raises(RuntimeError):
        mint.main(["--subject", "openclaw"])


def test_service_token_works_with_dev_login_off(monkeypatch, capsys):
    monkeypatch.setattr(settings, "ENABLE_DEV_AUTH", False)
    _, token = mint_token(capsys)
    assert client.post("/auth/token", json={"username": "x"}).status_code == 404
    response = client.post(
        "/project/design", json=INVALID_BODY, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 422


# --- what the smoke test relies on -------------------------------------------

def test_auth_is_checked_before_the_body():
    """The smoke test's probes are free only if auth fails before any run starts."""
    assert client.post("/project/design", json=INVALID_BODY).status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert client.post("/project/design", json=INVALID_BODY, headers=bad).status_code == 401


def test_smoke_http_checks_pass_on_a_safe_server(monkeypatch):
    token = "good"

    def fake_http(method, url, token=None, body=None):
        if url.endswith("/health"):
            return 200, "{}"
        if url.endswith("/auth/token"):
            return 404, ""
        return (422, "") if token == "good" else (401, "")

    monkeypatch.setattr(smoke, "http", fake_http)
    results = smoke.check_http("http://x", token)
    assert [r[1] for r in results] == ["PASS"] * 5


def test_smoke_flags_dev_login_and_missing_auth(monkeypatch):
    monkeypatch.setattr(smoke, "http", lambda method, url, token=None, body=None: (200, ""))
    outcomes = {name: outcome for name, outcome, _ in smoke.check_http("http://x", None)}
    assert outcomes["dev login is off"] == "FAIL"
    assert outcomes["no token -> 401"] == "FAIL"
    assert outcomes["service token accepted"] == "SKIP"


def test_a_failing_ss_is_a_failure(monkeypatch):
    class Result:
        returncode, stdout, stderr = 1, "", "Cannot open netlink socket"
    monkeypatch.setattr(smoke.sys, "platform", "linux")
    monkeypatch.setattr(smoke.shutil, "which", lambda name: "/usr/bin/ss")
    monkeypatch.setattr(smoke.Path, "exists", lambda self: False)
    monkeypatch.setattr(smoke.subprocess, "run", lambda *a, **k: Result())
    assert smoke.check_listeners({22})[1] == "FAIL"


def test_unreachable_server_fails_not_passes():
    status, _ = smoke.http("GET", "http://127.0.0.1:9/health")
    assert status == 0


SS_OUTPUT = """\
LISTEN 0 4096 127.0.0.1:8000 0.0.0.0:*
LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*
LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 128 [::]:22 [::]:*
LISTEN 0 4096 [::1]:11434 [::]:*
LISTEN 0 4096 *:5432 *:*
LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*
LISTEN 0 4096 100.101.102.103:18789 0.0.0.0:*
LISTEN 0 4096 [fd7a:115c:a1e0::1]:443 [::]:*
LISTEN 0 4096 10.0.0.5:9090 0.0.0.0:*
"""


def test_public_listeners_ignore_loopback_and_tailscale():
    found = smoke.public_listeners(SS_OUTPUT)
    assert ("0.0.0.0", 22) in found and ("[::]", 22) in found
    assert ("*", 5432) in found and ("0.0.0.0", 8000) in found
    # A VCN private address is still reachable by anything the security list allows.
    assert ("10.0.0.5", 9090) in found
    assert all(port not in (53, 11434, 18789, 443) for _, port in found)
    assert ("127.0.0.1", 8000) not in found


# --- container files ----------------------------------------------------------

def test_compose_publishes_only_on_loopback_and_restarts():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for name, service in compose["services"].items():
        assert service.get("restart") == "unless-stopped", name
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} publicly"


def test_dockerfile_drops_root():
    lines = [l.strip() for l in (ROOT / "Dockerfile").read_text().splitlines()]
    users = [l for l in lines if l.startswith("USER ")]
    assert users and users[-1] not in ("USER root", "USER 0")
    assert lines.index(users[-1]) < next(i for i, l in enumerate(lines) if l.startswith("CMD"))
