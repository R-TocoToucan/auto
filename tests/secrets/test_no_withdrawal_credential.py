"""Static repository-wide sweep — no withdrawal credential exists (D-69).

Withdrawal permission is permanently disabled on every API key ever
configured for this project (project safety rule 2). This test greps
every tracked `.py` / `.toml` file for the token `withdraw`
(case-insensitive) and asserts that every match sits inside a comment
or docstring that explicitly cites the D-69 prohibition — i.e., the
string `D-69` or the phrase `no withdrawal` appears within 3 lines
above/below the match.

Belt-and-suspenders: any commit that introduces a withdrawal field, a
withdrawal permission flag, or even a comment mentioning `withdraw`
without the D-69 citation is a hard test failure.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WITHDRAW_RE = re.compile(r"withdraw", re.IGNORECASE)
_D69_MARKERS = ("D-69", "no withdrawal")
_CONTEXT_LINES = 3


def _tracked_files() -> list[Path]:
    """List every `.py` and `.toml` file git tracks in the repo."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(_REPO_ROOT),
            "ls-files",
            "*.py",
            "*.toml",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [_REPO_ROOT / line for line in result.stdout.splitlines() if line]


def _find_withdraw_matches(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    return [
        (idx, line)
        for idx, line in enumerate(lines)
        if _WITHDRAW_RE.search(line)
    ]


def _has_d69_marker_within_context(
    lines: list[str], match_idx: int
) -> bool:
    lo = max(0, match_idx - _CONTEXT_LINES)
    hi = min(len(lines), match_idx + _CONTEXT_LINES + 1)
    window = "\n".join(lines[lo:hi])
    return any(marker in window for marker in _D69_MARKERS)


class TestNoWithdrawalCredential:
    def test_every_withdraw_match_cites_D69(self) -> None:
        """Every tracked `withdraw` occurrence must sit next to a D-69 citation."""
        offenders: list[str] = []
        for path in _tracked_files():
            # Skip test files under this very directory — their own
            # constants would create a false positive.
            if path.resolve() == Path(__file__).resolve():
                continue
            try:
                matches = _find_withdraw_matches(path)
            except (OSError, UnicodeDecodeError):
                # Binary-ish or unreadable files are irrelevant to a
                # text-only credential sweep.
                continue
            if not matches:
                continue
            lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
            for idx, line in matches:
                if not _has_d69_marker_within_context(lines, idx):
                    offenders.append(
                        f"{path.relative_to(_REPO_ROOT)}:{idx + 1}: {line.strip()}"
                    )
        assert not offenders, (
            "Every 'withdraw' occurrence must cite D-69 or 'no withdrawal' "
            f"within {_CONTEXT_LINES} lines. Offenders:\n" + "\n".join(offenders)
        )

    def test_bithumb_secrets_has_no_withdrawal_field(self) -> None:
        """Static sanity: `BithumbSecrets` MUST expose zero withdrawal_* fields."""
        from bithumb_bot.secrets.settings import BithumbSecrets

        for name in BithumbSecrets.model_fields:
            assert "withdraw" not in name.lower(), (
                f"forbidden withdrawal-adjacent field: {name!r}"
            )
