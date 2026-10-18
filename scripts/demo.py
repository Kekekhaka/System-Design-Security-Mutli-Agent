import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.multi_agent_graph import build_multi_agent, initial_state

DEFAULT_REQUIREMENTS = (
    "Design a simple payment processing API where users can pay for "
    "items using a credit card."
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one design + security audit cycle from the CLI."
    )
    parser.add_argument("requirements", nargs="?", default=DEFAULT_REQUIREMENTS)
    args = parser.parse_args()

    graph = build_multi_agent()
    result = graph.invoke(initial_state(args.requirements))

    print("=" * 72)
    print("ARCHITECTURE")
    print("=" * 72)
    print(result.get("architecture"))
    print()
    print("=" * 72)
    print("API SPEC")
    print("=" * 72)
    print(result.get("api_spec"))
    print()
    print("=" * 72)
    print(f"SECURITY AUDIT (revisions: {result.get('revision_count')})")
    print("=" * 72)
    print(result.get("security_findings"))


if __name__ == "__main__":
    main()
