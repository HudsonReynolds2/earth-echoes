"""Close the warm test containers by hand (task INFRA.1).

    make testpool-down          # from the repository root
    uv run python -m tests.testpool --down

The pool exists so a gate run does not pay to start and destroy 57 Postgres
containers (see the pool section of `conftest.py`). Its containers close
themselves when a later run finds them idle past `POOL_IDLE_TTL`, which is the
normal path and needs nobody's attention. This is the other one: reclaiming the
RAM now, or clearing a server that has wedged badly enough that a run cannot use
it.

Removing a pooled container while another agent's gate is mid-run is safe in the
sense that nothing is corrupted — but that run will fail on the next database it
asks for, so `--down` is a deliberate act and not a routine cleanup.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402  (the path insert above is what makes this importable)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Close the warm test container pool (INFRA.1)")
    parser.add_argument(
        "--down",
        action="store_true",
        help="remove every pooled container now, idle or not",
    )
    args = parser.parse_args(argv)

    removed = conftest.reap_pool(force=args.down)
    if removed:
        print(f"removed {len(removed)} pooled container(s): {', '.join(removed)}")
    else:
        print("no pooled containers to remove")
    return 0


if __name__ == "__main__":
    sys.exit(main())
