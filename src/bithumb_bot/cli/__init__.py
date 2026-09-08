"""CLI dispatcher root.

The `bt` console script and its argparse dispatcher are wired in plan 01-03.
Phase-1 verbs only (D-86); future verbs (D-87) are registered but their handlers
raise `NotImplementedError` (D-90).
"""
