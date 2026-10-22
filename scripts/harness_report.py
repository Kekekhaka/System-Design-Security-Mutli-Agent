"""Report what Harness2 learned.

    python scripts/harness_report.py
    python scripts/harness_report.py --trend 20
    python scripts/harness_report.py --persona-id p_abc123 --json
    python scripts/harness_report.py --failures

Prints per-persona runs, wins, failures, mean revisions and mean weighted
findings; the champion and its promotion/rollback history; and the trend of the
mean weighted findings over the last runs, so "is it getting better?" has an
answer.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.harness.store import (
    StoreError,
    default_history_path,
    default_store_path,
    load_store,
    read_history,
    resolve_hmac_key,
)


def _personas(store: dict, persona_id: str | None) -> list[dict]:
    personas = store.get("personas", [])
    if persona_id:
        personas = [p for p in personas if p.get("id") == persona_id]
    return sorted(
        personas,
        key=lambda p: (
            (p.get("stats") or {}).get("mean_weighted") is None,
            (p.get("stats") or {}).get("mean_weighted") or 0,
            (p.get("stats") or {}).get("failures", 0),
        ),
    )


def _trend(history: list[dict], limit: int) -> list[float]:
    values: list[float] = []
    for entry in history[-limit:]:
        weighted = [
            item.get("weighted")
            for item in entry.get("candidates") or []
            if isinstance(item.get("weighted"), (int, float))
        ]
        if weighted:
            values.append(round(sum(weighted) / len(weighted), 1))
    return values


def build_report(store: dict, history: list[dict], args) -> dict:
    personas = []
    for persona in _personas(store, args.persona_id):
        stats = persona.get("stats") or {}
        personas.append(
            {
                "persona_id": persona.get("id"),
                "origin": persona.get("origin"),
                "status": persona.get("status"),
                "runs": stats.get("runs", 0),
                "wins": stats.get("wins", 0),
                "approvals": stats.get("approvals", 0),
                "failures": stats.get("failures", 0),
                "mean_revisions": stats.get("mean_revisions"),
                "mean_weighted": stats.get("mean_weighted"),
                "weakness_categories": stats.get("weakness_categories", []),
                "last_run": stats.get("last_run"),
            }
        )
    champion = store.get("champion") or {}
    report = {
        "personas": personas,
        "champion": champion.get("persona_id"),
        "champion_history": store.get("champion_history", []),
        "trend": _trend(history, args.last_runs),
    }
    if args.trend:
        report["trend"] = _trend(history, args.trend)
    return report


def _failures(history: list[dict]) -> list[dict]:
    rows = []
    for entry in history:
        for item in entry.get("candidates") or []:
            if item.get("outcome") == "failed":
                rows.append(
                    {
                        "run_id": entry.get("run_id"),
                        "timestamp": entry.get("timestamp"),
                        "task_hash": entry.get("task_hash"),
                        "persona_id": item.get("persona_id"),
                        "failure_reason": item.get("failure_reason"),
                    }
                )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report Harness2 persona statistics.")
    parser.add_argument("--persona-id")
    parser.add_argument("--last-runs", type=int, default=100)
    parser.add_argument("--failures", action="store_true", help="Show only failed candidates")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--trend", type=int, help="Mean weighted findings over the last N runs")
    args = parser.parse_args(argv)

    store_path = default_store_path()
    key = resolve_hmac_key()
    try:
        store = load_store(store_path, key=key)
        history = read_history(
            default_history_path(store_path), key=key, limit=max(0, args.last_runs)
        )
    except StoreError as exc:
        print(f"Report aborted: {exc}", file=sys.stderr)
        return 4

    if args.failures:
        rows = _failures(history)
        if args.json:
            print(json.dumps({"failures": rows}, indent=2))
        else:
            if not rows:
                print("No failed candidates in the last runs.")
            for row in rows:
                print(
                    f"{row['timestamp']}  {row['persona_id']}  "
                    f"{row['failure_reason']}  run={row['run_id']}  task={row['task_hash'][:12]}"
                )
        return 0

    report = build_report(store, history, args)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    if not report["personas"]:
        print("No personas in the library.")
        return 0
    print(f"{'persona':>16} {'origin':>9} {'status':>8} {'runs':>5} {'wins':>5} "
          f"{'fail':>5} {'mean rev':>9} {'mean wt':>8}")
    for row in report["personas"]:
        mean_rev = "-" if row["mean_revisions"] is None else f"{row['mean_revisions']:.2f}"
        mean_wt = "-" if row["mean_weighted"] is None else f"{row['mean_weighted']:.1f}"
        print(
            f"{row['persona_id']:>16} {row['origin'] or '-':>9} {row['status'] or '-':>8} "
            f"{row['runs']:>5} {row['wins']:>5} {row['failures']:>5} "
            f"{mean_rev:>9} {mean_wt:>8}"
        )
    print(f"\nChampion: {report['champion'] or '(none)'}")
    for event in report["champion_history"]:
        print(
            f"  {event.get('at')}  {event.get('action')}: "
            f"{event.get('persona_id')}  {event.get('reason') or ''}"
        )
    trend = report["trend"]
    print("Trend (mean weighted findings, last runs): "
          + (" -> ".join(str(value) for value in trend) if trend else "(no scored runs yet)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
