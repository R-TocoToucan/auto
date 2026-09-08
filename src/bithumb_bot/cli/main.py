"""`bt` console-script entry point (D-85 / D-99).

Bound by ``[project.scripts] bt = "bithumb_bot.cli.main:main"`` in
``pyproject.toml``. Thin adapter: delegates to
:func:`bithumb_bot.cli.dispatcher.dispatch` and converts argparse's
``SystemExit`` into a return code so the console-script wrapper writes
the correct process exit status.

Also usable as ``python -m bithumb_bot.cli.main`` (`__main__` block at
the bottom) so operators / tests can invoke without needing the console
script installed in their PATH.
"""

from __future__ import annotations

import sys
from typing import Sequence

from bithumb_bot.cli import dispatcher


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point — parse argv, dispatch, return the exit code.

    Args:
        argv: Command-line arguments EXCLUDING the program name. When
            ``None`` (the default), ``sys.argv[1:]`` is used. Explicit
            argv is the test-friendly path.

    Returns:
        Integer process exit code — ``0`` on success, ``1`` on a
        documented refusal, ``2`` on an argparse usage error, other
        codes possible for handler-specific failures.

    Note:
        Argparse's ``--help`` / ``--version`` actions raise
        ``SystemExit(0)`` from inside ``parser.parse_args()``; this
        function does NOT catch that — the console script wrapper
        propagates it as a clean process exit. Test callers wrap
        ``main`` in ``pytest.raises(SystemExit)`` for those cases.
    """
    argv_list: list[str] = list(argv) if argv is not None else list(sys.argv[1:])
    return dispatcher.dispatch(argv_list)


if __name__ == "__main__":
    # `python -m bithumb_bot.cli.main [...]` entry.
    try:
        sys.exit(main())
    except SystemExit:
        raise
