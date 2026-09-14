"""Reject QQ email identities from any published Git history."""

from __future__ import annotations

import re
import subprocess
import sys


FORBIDDEN = re.compile(r"@qq\.com$", re.IGNORECASE)


def main() -> int:
    history_args = sys.argv[1:] or ["--all"]
    result = subprocess.run(
        ["git", "log", *history_args, "--format=%H%x09%ae"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        print(result.stderr.strip() or "Unable to inspect Git history.", file=sys.stderr)
        return result.returncode

    leaked = []
    for line in result.stdout.splitlines():
        commit, _, email = line.partition("\t")
        if FORBIDDEN.search(email):
            leaked.append((commit, email))

    if leaked:
        print("Privacy check failed: personal QQ identity found in Git history:")
        for commit, email in leaked:
            print(f"  {commit} {email}")
        return 1

    print("Privacy check passed: no forbidden QQ identity in reachable Git history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
