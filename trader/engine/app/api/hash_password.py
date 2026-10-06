"""Print a bcrypt hash for the panel password.

    python -m app.api.hash_password "<password>"
    python -m app.api.hash_password            # prompts (no echo)

Put the result in .env as PANEL_PASSWORD_HASH. Keep the single quotes: the hash
contains "$" characters that docker compose would otherwise try to interpolate.
"""
from __future__ import annotations

import getpass
import sys

from app.api.passwords import hash_password


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        password = argv[1]
    elif not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Again: "):
            print("passwords do not match", file=sys.stderr)
            return 2

    if not password:
        print("empty password", file=sys.stderr)
        return 2
    if len(password.encode("utf-8")) > 72:
        print("warning: bcrypt only uses the first 72 bytes of the password", file=sys.stderr)

    hashed = hash_password(password)
    print(hashed)
    print(f"\nAdd to .env (single quotes matter):\nPANEL_PASSWORD_HASH='{hashed}'", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
