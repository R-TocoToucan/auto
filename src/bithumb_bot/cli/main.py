"""`bt` console-script entry point (D-85 / D-99).

Bound by ``[project.scripts] bt = "bithumb_bot.cli.main:main"`` in
``pyproject.toml``. Thin adapter: delegates to
:func:`bithumb_bot.cli.dispatcher.dispatch` and converts argparse's
``SystemExit`` into a return code so the console-script wrapper writes
the correct process exit status.

Also usable as ``python -m bithumb_bot.cli.main`` (`__main__` block at
the bottom) so operators / tests can invoke without needing the console
script installed in their PATH.

Structured logging (plan 01-04):

* :func:`configure_logging` is called BEFORE dispatch so any log line
  from a handler (or from an unhandled exception below) flows through
  the D-70 ``redact_secrets`` processor.
* The renderer defaults to `console` for TTY, `json` when stderr is
  not a TTY (CI / piped runs).
* Explicit `--log-format {json,console}` overrides the TTY detection.
* The dispatch call is wrapped so an unhandled exception is
  structured-logged before propagating.
"""

from __future__ import annotations

import sys
from typing import Sequence

from bithumb_bot.cli import dispatcher


def _detect_log_format(argv: list[str]) -> str:
    """Detect explicit `--log-format` flag; default per TTY-ness of stderr.

    Kept as a small, argparse-free scan so it never disturbs the
    argparse tree the dispatcher builds. The dispatcher's argparse
    still sees `--log-format` on the argv (unknown top-level flag →
    handled as a two-level subparser miss OR ignored per subparser
    definition — for Phase 1 the flag is stripped here so the
    dispatcher's tree does not need to know about it).
    """
    fmt: str | None = None
    filtered: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--log-format" and i + 1 < len(argv):
            fmt = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--log-format="):
            fmt = tok.split("=", 1)[1]
            i += 1
            continue
        filtered.append(tok)
        i += 1
    argv[:] = filtered  # in-place modification consumed by caller
    if fmt is not None:
        return fmt
    try:
        is_tty = sys.stderr.isatty()
    except (AttributeError, ValueError):  # pragma: no cover — defensive
        is_tty = False
    return "console" if is_tty else "json"


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

    # Extract `--log-format` (strips it from argv_list) and wire structlog
    # BEFORE dispatch — every log line in the handler chain flows through
    # `redact_secrets`. Lazy import so `bt --help` remains side-effect-free.
    fmt = _detect_log_format(argv_list)
    try:
        from bithumb_bot.observability.logging import configure_logging

        configure_logging(fmt)  # type: ignore[arg-type]
    except Exception:  # pragma: no cover — never let logging block dispatch
        pass

    try:
        return dispatcher.dispatch(argv_list)
    except SystemExit:
        # argparse --help / --version / usage error — propagate.
        raise
    except Exception as exc:
        try:
            import structlog

            structlog.get_logger().error(
                "bt.unhandled",
                error_class=type(exc).__name__,
            )
        except Exception:  # pragma: no cover — never let logging block reporting
            pass
        print(
            f"bt: unhandled error ({type(exc).__name__}). "
            "See structured logs for details.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    # `python -m bithumb_bot.cli.main [...]` entry.
    try:
        sys.exit(main())
    except SystemExit:
        raise
