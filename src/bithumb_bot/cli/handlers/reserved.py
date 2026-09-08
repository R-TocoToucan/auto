"""Shared handler for every D-87 reserved-verb subparser (D-90 discipline).

The single :func:`reserved_handler` prints a uniform refusal message to
stderr and returns ``1``. It DOES NOT raise ``NotImplementedError`` at
runtime — raising would emit an unclean Python traceback for what is a
documented refusal. Return-code refusal preserves the operator's
signal-to-noise ratio.

D-90 discipline: reserved future verbs must not be scaffolded as
functional. Every D-87 (verb, subverb) is bound to this same function via
:data:`bithumb_bot.cli.dispatcher.HANDLER_MAP`; no D-87 verb has its own
scaffolded module. When a reserved verb becomes real (in a future phase),
its dispatcher wiring changes to the new handler — this module stays put.
"""

from __future__ import annotations

import argparse
import sys


_RESERVED_MESSAGE_TEMPLATE = (
    "bt {verb} {subverb}: refused — this verb is reserved for a future "
    "phase — do not scaffold. Registered in the capability registry per "
    "D-87 so its prerequisites exist as data, but no functional handler "
    "is provided in Phase 1 (D-90 phase discipline)."
)


def reserved_handler(args: argparse.Namespace) -> int:
    """Refuse uniformly for every D-87 reserved (verb, subverb) pair.

    Prints the reserved-message to stderr and returns ``1``. No side
    effects — no file writes, no network calls, no credential loads.
    """
    verb = getattr(args, "verb", "<verb>")
    subverb = getattr(args, "subverb", "<subverb>")
    print(
        _RESERVED_MESSAGE_TEMPLATE.format(verb=verb, subverb=subverb),
        file=sys.stderr,
    )
    return 1


__all__ = ["reserved_handler"]
