"""Mint a long-lived bearer token for a service such as OpenClaw.

Production runs with ENABLE_DEV_AUTH=false, so /auth/token is closed and the
normal 60-minute tokens cannot be minted. A service gets its own token here,
signed with the same JWT_SECRET. Its subject is "svc:<name>", a namespace no
user can take, so a service only ever sees its own sessions.

Run it where the app's .env is loaded (on the server, inside the container):

    docker compose exec api python scripts/mint_service_token.py --subject openclaw --out /app/data/openclaw.token

With --out the token goes to a new file readable only by its owner and never
to the screen; without it, the token is printed once. Store it in the
service's own secret store, not in this repo. /app/data is a persisted
volume the API also writes to: move the file out and delete it once stored.

Revoke one token: add its id (printed when minted) to REVOKED_TOKEN_IDS in
.env and restart. Revoke every token: rotate JWT_SECRET and restart.
"""

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.security import SERVICE_SUBJECT_PREFIX, create_access_token  # noqa: E402

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_DAYS = 30


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subject", required=True, help="service name, e.g. openclaw")
    parser.add_argument(
        "--days", type=int, default=MAX_DAYS, help=f"lifetime in days (1-{MAX_DAYS}, default {MAX_DAYS})"
    )
    parser.add_argument("--out", help="write the token to this new file (mode 0600) instead of stdout")
    args = parser.parse_args(argv)
    if not NAME_PATTERN.match(args.subject):
        parser.error("--subject must be 1-64 letters, digits, '_' or '-'")
    if not 1 <= args.days <= MAX_DAYS:
        parser.error(f"--days must be between 1 and {MAX_DAYS}")
    return args


def write_secret_file(path: str, content: str) -> None:
    """Create the file with owner-only permissions; never overwrite an existing one."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content + "\n")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    token_id = secrets.token_hex(8)
    token = create_access_token(
        SERVICE_SUBJECT_PREFIX + args.subject,
        expires_minutes=args.days * 24 * 60,
        token_id=token_id,
    )
    if args.out:
        try:
            write_secret_file(args.out, token)
        except FileExistsError:
            sys.exit(f"{args.out} already exists; choose a new path or delete it first")
    else:
        print(token)
    print(
        f"subject={SERVICE_SUBJECT_PREFIX}{args.subject} token_id={token_id} "
        f"expires_in_days={args.days}" + (f" written_to={args.out}" if args.out else ""),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
