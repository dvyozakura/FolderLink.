"""`python -m folderlink` の入口。"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from folderlink.app import run

    return run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
