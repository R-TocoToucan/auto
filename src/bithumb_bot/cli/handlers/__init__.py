"""CLI handler modules — one per Phase-1 D-86 verb, plus the shared
reserved-verb handler (D-87 / D-90) added in plan 01-03-08.

**Defense-in-depth (D-85)**: every handler function's FIRST statement
calls ``validate(capability)``. Direct Python callers that bypass the
CLI (`from bithumb_bot.cli.handlers.m0_selfcheck import handler`) still
trip the guard — no code path reaches the operational body without a
passing capability check.

The dispatcher (``bithumb_bot.cli.dispatcher``) wires every registered
capability to a handler via its own ``HANDLER_MAP``. Individual handler
modules land in later tasks:

* ``config_validate`` — plan 01-03-06
* ``m0_selfcheck``    — plan 01-03-07
* ``reserved``        — plan 01-03-08
* ``m1_stubs``        — plan 01-03-09
"""
