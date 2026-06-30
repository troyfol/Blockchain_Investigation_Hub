"""Store an API key in the OS keyring via a hidden prompt (so it never lands in shell history).

    python scripts/set_key.py            # stores the 'etherscan' key
    python scripts/set_key.py defillama  # stores some other named secret

The key is read with getpass (input hidden) and written to the OS keyring only — never to disk.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `backend.app...` imports

from backend.app.secrets import get_secret, set_secret  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    name = args[0] if args else "etherscan"
    try:
        key = getpass.getpass(f"Paste the {name!r} API key (input is hidden), then press Enter: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled — nothing stored.")
        return 1
    if not key:
        print("no key entered — nothing stored.")
        return 1
    set_secret(name, key)
    stored = get_secret(name)
    print(f"OK — stored {name!r} in the OS keyring (length {len(stored or '')}). You can close this window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
