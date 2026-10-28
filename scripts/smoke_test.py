"""Post-deploy smoke test: one command, one PASS/FAIL line per check.

On the server (plain python3, no app dependencies needed):

    python3 scripts/smoke_test.py --token "$OPENCLAW_TOKEN"

Inside the container, the live check (one tiny model call, a fraction of a
cent, and the Postgres checkpoint tables):

    docker compose exec api python scripts/smoke_test.py --live

Checks that need something not given are reported as SKIP, never PASS.
Exit code: 0 if nothing failed, 1 otherwise.
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOOPBACK = re.compile(r"^(127\.\d+\.\d+\.\d+|\[?::1\]?|localhost)$")


def http(method: str, url: str, token: str | None = None, body: dict | None = None):
    """Return (status, text); status 0 means the server could not be reached."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as err:
        return 0, str(err)


def is_private(address: str) -> bool:
    """Loopback, or a Tailscale address (100.64.0.0/10, fd7a:115c:a1e0::/48)."""
    if LOOPBACK.match(address):
        return True
    try:
        ip = ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return False  # "*" and other wildcards are public
    return ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network("fd7a:115c:a1e0::/48")


def public_listeners(ss_output: str) -> list[tuple[str, int]]:
    """(address, port) of every TCP listener reachable from outside the server."""
    found = []
    for line in ss_output.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] != "LISTEN":
            continue
        address, _, port = fields[3].rpartition(":")
        address = address.split("%")[0]
        if port.isdigit() and not is_private(address):
            found.append((address, int(port)))
    return found


def check_http(base: str, token: str | None) -> list[tuple[str, str, str]]:
    results = []
    status, _ = http("GET", f"{base}/health")
    results.append(("health answers", "PASS" if status == 200 else "FAIL", f"HTTP {status}"))

    status, _ = http("POST", f"{base}/auth/token", body={"username": "smoke"})
    results.append(
        ("dev login is off", "PASS" if status == 404 else "FAIL", f"/auth/token -> HTTP {status}")
    )

    # An invalid session_id is rejected (422) only after auth passes, so these
    # checks never start a paid design run.
    invalid = {"requirements": "smoke", "session_id": "not:valid"}
    status, _ = http("POST", f"{base}/project/design", body=invalid)
    results.append(("no token -> 401", "PASS" if status == 401 else "FAIL", f"HTTP {status}"))
    status, _ = http("POST", f"{base}/project/design", token="not-a-token", body=invalid)
    results.append(("bad token -> 401", "PASS" if status == 401 else "FAIL", f"HTTP {status}"))

    if token:
        status, _ = http("POST", f"{base}/project/design", token=token, body=invalid)
        results.append(
            ("service token accepted", "PASS" if status == 422 else "FAIL", f"HTTP {status} (422 expected)")
        )
    else:
        results.append(("service token accepted", "SKIP", "pass --token to check"))
    return results


def check_listeners(allowed_ports: set[int]) -> tuple[str, str, str]:
    name = "only allowed ports are public"
    if not sys.platform.startswith("linux") or not shutil.which("ss"):
        return name, "SKIP", "needs Linux with ss (run on the server, not in the container)"
    if Path("/.dockerenv").exists():
        return name, "SKIP", "inside a container; run this check on the server itself"
    result = subprocess.run(["ss", "-Htln"], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        # An empty listing would otherwise look like a clean host.
        return name, "FAIL", f"ss failed or listed nothing: {result.stderr.strip()[:200]}"
    exposed = sorted({(a, p) for a, p in public_listeners(result.stdout) if p not in allowed_ports})
    if exposed:
        return name, "FAIL", "public: " + ", ".join(f"{a}:{p}" for a, p in exposed)
    return name, "PASS", f"allowed: {sorted(allowed_ports)}"


def check_live() -> list[tuple[str, str, str]]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.core.config import settings
    from src.core.llm import get_llm, invoke_checked

    results = []
    try:
        reply = invoke_checked(
            get_llm(max_tokens=50, reasoning_effort="none"),
            [("user", "Reply with exactly: OK")],
            stage="smoke-test",
        )
        text = getattr(reply, "content", reply)
        ok = "OK" in str(text)
        results.append(("model call", "PASS" if ok else "FAIL", f"{settings.OPENROUTER_MODEL}: {str(text)[:40]!r}"))
    except Exception as exc:
        results.append(("model call", "FAIL", f"{type(exc).__name__}: {str(exc)[:200]}"))

    if not settings.DATABASE_URL:
        results.append(("checkpoint tables", "SKIP", "DATABASE_URL is not set"))
        return results
    try:
        import psycopg

        with psycopg.connect(settings.DATABASE_URL, connect_timeout=5) as conn:
            exists = conn.execute("SELECT to_regclass('public.checkpoints')").fetchone()[0]
            if not exists:
                results.append(("checkpoint tables", "FAIL", "no checkpoints table (has the API started?)"))
            else:
                rows = conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
                results.append(("checkpoint tables", "PASS", f"{rows} checkpoint rows"))
    except Exception as exc:
        results.append(("checkpoint tables", "FAIL", f"{type(exc).__name__}: {str(exc)[:200]}"))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post-deploy smoke test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.environ.get("SMOKE_TOKEN"), help="a service token (or SMOKE_TOKEN)")
    parser.add_argument("--allow-port", type=int, action="append", default=None,
                        help="a port allowed on public interfaces (default: 22)")
    parser.add_argument("--live", action="store_true", help="model call and Postgres check (run in the container)")
    args = parser.parse_args(argv)

    if args.live:
        results = check_live()
    else:
        results = check_http(args.base_url.rstrip("/"), args.token)
        results.append(check_listeners(set(args.allow_port or [22])))

    for name, outcome, detail in results:
        print(f"{outcome:<4}  {name:<32} {detail}")
    counts = {o: sum(outcome == o for _, outcome, _ in results) for o in ("PASS", "SKIP", "FAIL")}
    failed = counts["FAIL"]
    print(f"\n{counts['PASS']} passed, {counts['SKIP']} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
