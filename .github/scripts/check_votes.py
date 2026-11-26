"""Check every data/wishlist/**/votes.txt after a push to master.

Report only: this script never writes, reverts or commits anything.

Expected format (what the backend vote writer produces): one lowercase GitHub
login per line, unique, sorted in byte order, each line ending in "\\n". A
zero-byte file means zero votes.

Exit status: 1 if any problem was found, otherwise 0.
"""
import re
import sys
from pathlib import Path

LOGIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?")


def check_votes_text(text: str) -> list[str]:
    if text == "":
        return []
    problems = []
    if not text.endswith("\n"):
        problems.append("file does not end with a newline")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # artifact of the trailing newline

    kept = []
    for number, line in enumerate(lines, start=1):
        if line == "":
            problems.append(f"line {number}: blank line")
        elif LOGIN_RE.fullmatch(line) is None:
            problems.append(f"line {number}: invalid login {line!r}")
        else:
            kept.append(line)

    seen = set()
    reported = set()
    unique = []
    for login in kept:
        if login in seen:
            if login not in reported:
                problems.append(f"duplicate login {login!r}")
                reported.add(login)
        else:
            seen.add(login)
            unique.append(login)

    if unique != sorted(unique):
        problems.append("logins are not sorted in byte order")
    return problems


def main(root=None) -> int:
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    base = root / "data" / "wishlist"
    if not base.is_dir():
        return 0
    found = False
    for path in sorted(base.rglob("votes.txt")):
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        for problem in check_votes_text(text):
            print(f"{path.relative_to(root).as_posix()}: {problem}")
            found = True
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
