"""Show, promote or roll back the Harness2 champion.

    python scripts/harness_champion.py show
    python scripts/harness_champion.py show --json
    python scripts/harness_champion.py promote p_abc123 --yes --reason "3 clean wins"
    python scripts/harness_champion.py rollback --yes

Promotion is manual and requires all of: three wins against the current
champion on at least two different tasks (from the run history), and a
golden-fixture check (scripts/eval_security_agent.py logic, reused) showing no
loss of planted-flaw recall and no increase in hallucinated findings. If the
check cannot run, there is no promotion.

Exit codes: 0 success, 2 validation, 5 promotion requirements not met.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.harness.champion import PromotionError, promote, rollback
from src.harness.store import (
    StoreError,
    default_history_path,
    default_store_path,
    load_store,
    read_history,
    resolve_hmac_key,
    save_store,
)


def _load():
    store_path = default_store_path()
    key = resolve_hmac_key()
    store = load_store(store_path, key=key)
    history = read_history(default_history_path(store_path), key=key)
    return store, history, store_path, key


def _show(args) -> int:
    store, _, _, _ = _load()
    champion = store.get("champion")
    payload = {"champion": (champion or {}).get("persona_id")}
    if champion:
        payload.update(
            {
                "since": champion.get("since"),
                "previous_champion": champion.get("previous_persona_id"),
                "prompt_sha256": champion.get("prompt_sha256"),
                "reason": champion.get("reason"),
            }
        )
    if args.json:
        print(json.dumps(payload, indent=2))
    elif not champion:
        print("No champion. Promote one with: harness_champion.py promote <persona_id> --yes")
    else:
        print(f"Champion: {payload['champion']}  (since {payload.get('since')})")
        print(f"  previous: {payload.get('previous_champion')}")
        print(f"  reason:   {payload.get('reason')}")
        print(f"  sha256:   {payload.get('prompt_sha256')}")
    return 0


def _promote(args) -> int:
    if not args.yes:
        print("Refusing to promote without --yes.", file=sys.stderr)
        return 2
    store, history, store_path, key = _load()
    try:
        result = promote(store, args.persona_id, args.reason or "", history)
    except PromotionError as exc:
        print(f"Not promoted: {exc}", file=sys.stderr)
        return 5
    except StoreError as exc:
        print(f"Promotion aborted: {exc}", file=sys.stderr)
        return 4
    save_store(store, store_path, key=key)
    print(json.dumps(result, indent=2))
    return 0


def _rollback(args) -> int:
    if not args.yes:
        print("Refusing to roll back without --yes.", file=sys.stderr)
        return 2
    store, _, store_path, key = _load()
    result = rollback(store, args.reason or "")
    save_store(store, store_path, key=key)
    print(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the Harness2 champion persona.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show", help="Show the champion")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_show)

    promote_parser = subparsers.add_parser("promote", help="Promote a persona")
    promote_parser.add_argument("persona_id")
    promote_parser.add_argument("--yes", action="store_true", help="Required confirmation")
    promote_parser.add_argument("--reason", help="Operator note for the audit trail")
    promote_parser.set_defaults(func=_promote)

    rollback_parser = subparsers.add_parser("rollback", help="Restore the previous champion")
    rollback_parser.add_argument("--yes", action="store_true", help="Required confirmation")
    rollback_parser.add_argument("--reason", help="Operator note for the audit trail")
    rollback_parser.set_defaults(func=_rollback)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except StoreError as exc:
        print(f"Champion command aborted: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
