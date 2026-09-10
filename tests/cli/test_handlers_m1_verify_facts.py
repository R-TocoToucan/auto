"""Tests for `bithumb_bot.cli.handlers.m1_verify_facts.handler`."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from bithumb_bot.bithumb_spec.verification import (
    FIVE_BUILD_TIME_FACTS,
    write_verification_bundle,
)
from bithumb_bot.cli.main import main
from bithumb_bot.config.validator import REPO_ROOT_ENV


def _make_all_confirmed_bundle(tmp_path: Path) -> Path:
    from bithumb_bot.artifact.canonical import write_with_sidecar

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    snap = bundle_dir / "snap.json"
    write_with_sidecar(snap, b'{"ok":true}\n')
    md_path, _ = write_verification_bundle(
        bundle_dir, snapshot_path=snap, fixture_paths=[]
    )
    text = md_path.read_text(encoding="utf-8")
    text = text.replace(
        "**Status:** [ ] confirmed  [ ] contradicted  [ ] unresolved",
        "**Status:** [x] confirmed  [ ] contradicted  [ ] unresolved",
    )
    text = text.replace(
        "**User approval status: human-approved by:** ",
        "**User approval status: human-approved by:** op-test",
    )
    md_path.write_text(text, encoding="utf-8")
    return bundle_dir


@pytest.fixture()
def _env(
    tmp_path: Path,
    tmp_gate1_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))
    monkeypatch.delenv("BITHUMB_TRADE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("BITHUMB_TRADE_SECRET_KEY", raising=False)
    return tmp_path


class TestVerifyFactsHandler:
    def test_confirmed_bundle_returns_zero(
        self,
        _env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bundle_dir = _make_all_confirmed_bundle(tmp_path)
        rc = main(["m1", "verify-facts", "--bundle", str(bundle_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "human-approved" in out

    def test_unresolved_bundle_returns_nonzero_and_names_fact(
        self,
        _env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from bithumb_bot.artifact.canonical import write_with_sidecar

        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        snap = bundle_dir / "snap.json"
        write_with_sidecar(snap, b'{"ok":true}\n')
        write_verification_bundle(bundle_dir, snapshot_path=snap, fixture_paths=[])
        # DO NOT tick any box.
        rc = main(["m1", "verify-facts", "--bundle", str(bundle_dir)])
        assert rc != 0
        err = capsys.readouterr().err
        # Some fact name from the required set must appear.
        assert any(f.key in err for f in FIVE_BUILD_TIME_FACTS)

    def test_missing_bundle_arg_returns_nonzero(
        self, _env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["m1", "verify-facts"])
        assert rc != 0

    def test_handler_source_never_imports_bithumbsecrets(self) -> None:
        """D-89: offline verification handler code MUST NOT construct
        `BithumbSecrets` or call `load_secrets`. Static source check."""
        import inspect

        import bithumb_bot.cli.handlers.m1_verify_facts as mod

        src = inspect.getsource(mod)
        # Look for CALLS / imports, not docstring mentions.
        assert "BithumbSecrets(" not in src
        assert "load_secrets(" not in src
        assert "from bithumb_bot.secrets" not in src
        assert "import bithumb_bot.secrets" not in src


class TestUnhandledExceptions:
    def test_generic_exception_returns_one_with_clean_error(
        self,
        _env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bundle_dir = _make_all_confirmed_bundle(tmp_path)
        with mock.patch(
            "bithumb_bot.bithumb_spec.verification.verify_facts_bundle",
            side_effect=RuntimeError("something-generic"),
        ):
            rc = main(["m1", "verify-facts", "--bundle", str(bundle_dir)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "RuntimeError" in err
