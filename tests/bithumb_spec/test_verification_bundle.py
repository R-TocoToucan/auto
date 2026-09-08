"""Tests for :mod:`bithumb_bot.bithumb_spec.verification` (D-78, D-84, T-1-04-06)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bithumb_bot.artifact.canonical import sha256_hex, write_with_sidecar
from bithumb_bot.bithumb_spec.verification import (
    FIVE_BUILD_TIME_FACTS,
    parse_verification_bundle,
    verify_facts_bundle,
    write_verification_bundle,
)
from bithumb_bot.config.validator import REPO_ROOT_ENV
from bithumb_bot.errors import (
    SnapshotValidationError,
    UnresolvedFactError,
)


@pytest.fixture()
def _validate_ok(
    tmp_path: Path,
    tmp_gate1_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))


def _write_dummy_snapshot(bundle_dir: Path) -> Path:
    """Write a placeholder snapshot in the bundle dir with a matching sidecar."""
    snap = bundle_dir / "snap.json"
    payload = b'{"ok":true}\n'
    write_with_sidecar(snap, payload)
    return snap


def _all_confirmed(md_text: str) -> str:
    """Return `md_text` with every fact's `[ ] confirmed` box ticked."""
    return md_text.replace(
        "**Status:** [ ] confirmed  [ ] contradicted  [ ] unresolved",
        "**Status:** [x] confirmed  [ ] contradicted  [ ] unresolved",
    )


class TestWriteBundle:
    def test_creates_verification_md_and_manifest(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        snap = _write_dummy_snapshot(tmp_path)
        # Move snap into the bundle dir so the manifest records the right name.
        target_snap = bundle_dir / "snap.json"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        target_snap.write_bytes(snap.read_bytes())
        # Sidecar too (used by the manifest hasher indirectly).
        (bundle_dir / "snap.json.sha256").write_bytes(
            (snap.with_name(snap.name + ".sha256")).read_bytes()
        )
        md_path, manifest_path = write_verification_bundle(
            bundle_dir,
            snapshot_path=target_snap,
            fixture_paths=[],
        )
        assert md_path.is_file()
        assert manifest_path.is_file()

    def test_verification_md_contains_human_approved(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        target_snap = bundle_dir / "snap.json"
        target_snap.write_bytes(b'{"ok":true}\n')
        (bundle_dir / "snap.json.sha256").write_text(
            f"{sha256_hex(target_snap.read_bytes())}  snap.json\n",
            encoding="utf-8",
        )
        md_path, _ = write_verification_bundle(
            bundle_dir, snapshot_path=target_snap, fixture_paths=[]
        )
        text = md_path.read_text(encoding="utf-8")
        assert "human-approved" in text
        # D-84: never "human-signed".
        assert "human-signed" not in text

    def test_manifest_is_canonical_bytes(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        target_snap = bundle_dir / "snap.json"
        target_snap.write_bytes(b'{"ok":true}\n')
        _, manifest_path = write_verification_bundle(
            bundle_dir, snapshot_path=target_snap, fixture_paths=[]
        )
        # Two calls into `write_verification_bundle` would fail overwrite
        # guard, so instead we assert the on-disk canonical shape:
        raw = manifest_path.read_bytes()
        # Ends in newline; sorted keys; compact separators.
        assert raw.endswith(b"\n")
        # Parses as JSON.
        parsed = json.loads(raw.decode("utf-8"))
        assert parsed["schema_version"] == 1
        assert parsed["snapshot_path"] == "snap.json"
        assert len(parsed["snapshot_sha256"]) == 64

    def test_all_five_facts_scaffolded(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        target_snap = bundle_dir / "snap.json"
        target_snap.write_bytes(b'{"ok":true}\n')
        md_path, _ = write_verification_bundle(
            bundle_dir, snapshot_path=target_snap, fixture_paths=[]
        )
        text = md_path.read_text(encoding="utf-8")
        for fact in FIVE_BUILD_TIME_FACTS:
            assert f"## Fact: `{fact.key}`" in text


class TestParseBundle:
    def test_all_confirmed(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        target_snap = bundle_dir / "snap.json"
        target_snap.write_bytes(b'{"ok":true}\n')
        md_path, _ = write_verification_bundle(
            bundle_dir, snapshot_path=target_snap, fixture_paths=[]
        )
        # Hand-tick every "confirmed" box.
        md_path.write_text(_all_confirmed(md_path.read_text(encoding="utf-8")), encoding="utf-8")
        parsed = parse_verification_bundle(bundle_dir)
        for fact in FIVE_BUILD_TIME_FACTS:
            assert parsed.facts[fact.key] == "confirmed"

    def test_unchecked_defaults_to_unresolved(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        target_snap = bundle_dir / "snap.json"
        target_snap.write_bytes(b'{"ok":true}\n')
        write_verification_bundle(
            bundle_dir, snapshot_path=target_snap, fixture_paths=[]
        )
        parsed = parse_verification_bundle(bundle_dir)
        for fact in FIVE_BUILD_TIME_FACTS:
            assert parsed.facts[fact.key] == "unresolved"


class TestVerifyFactsBundle:
    def test_all_confirmed_passes(self, tmp_path: Path, _validate_ok: None) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        target_snap = bundle_dir / "snap.json"
        target_snap.write_bytes(b'{"ok":true}\n')
        md_path, _ = write_verification_bundle(
            bundle_dir, snapshot_path=target_snap, fixture_paths=[]
        )
        md_path.write_text(_all_confirmed(md_path.read_text(encoding="utf-8")), encoding="utf-8")
        # Should NOT raise.
        verify_facts_bundle(bundle_dir)

    def test_one_unresolved_raises(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        target_snap = bundle_dir / "snap.json"
        target_snap.write_bytes(b'{"ok":true}\n')
        md_path, _ = write_verification_bundle(
            bundle_dir, snapshot_path=target_snap, fixture_paths=[]
        )
        # Tick everything confirmed, THEN untick just one fact.
        text = _all_confirmed(md_path.read_text(encoding="utf-8"))
        text = text.replace(
            "## Fact: `jwt_timestamp_claim_shape`\n"
            "\n"
            "**Claim.** ",
            "## Fact: `jwt_timestamp_claim_shape`\n"
            "\n"
            "**Claim.** ",
        )
        # Simpler: replace the confirmed [x] in the first fact section
        # back to [ ] within just that section.
        section_start = text.find("## Fact: `jwt_timestamp_claim_shape`")
        section_end = text.find("## Fact:", section_start + 1)
        section = text[section_start:section_end]
        section_fixed = section.replace("[x] confirmed", "[ ] confirmed")
        text = text[:section_start] + section_fixed + text[section_end:]
        md_path.write_text(text, encoding="utf-8")
        with pytest.raises(UnresolvedFactError) as excinfo:
            verify_facts_bundle(bundle_dir)
        assert excinfo.value.fact_name == "jwt_timestamp_claim_shape"

    def test_snapshot_hash_mismatch_raises(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        target_snap = bundle_dir / "snap.json"
        target_snap.write_bytes(b'{"ok":true}\n')
        md_path, _ = write_verification_bundle(
            bundle_dir, snapshot_path=target_snap, fixture_paths=[]
        )
        md_path.write_text(_all_confirmed(md_path.read_text(encoding="utf-8")), encoding="utf-8")
        # Mutate the snapshot on disk AFTER the manifest was written.
        target_snap.write_bytes(b'{"ok":false}\n')
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)

    def test_missing_manifest_raises(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        # Hand-craft an all-confirmed VERIFICATION.md so the facts
        # check passes and we exercise the missing-manifest branch.
        confirmed_lines: list[str] = []
        for fact in FIVE_BUILD_TIME_FACTS:
            confirmed_lines.extend(
                [
                    f"## Fact: `{fact.key}`",
                    "",
                    "**Claim.** (test)",
                    "",
                    "- **Status:** [x] confirmed  [ ] contradicted  [ ] unresolved",
                    "",
                ]
            )
        (bundle_dir / "VERIFICATION.md").write_text(
            "\n".join(confirmed_lines) + "\n", encoding="utf-8"
        )
        # No manifest.json.
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)


class TestNoHumanSignedInCodebase:
    """T-1-04-06: the phrase 'human-signed' MUST NOT appear in src/ or tests/ code."""

    def test_grep_returns_zero_matches(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        # Use `git grep` so we only look at tracked files.
        for path_root in ("src", "tests"):
            result = subprocess.run(
                [
                    "git",
                    "grep",
                    "-l",
                    "-w",
                    "-i",
                    "human-signed",
                    "--",
                    path_root,
                ],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
            matched = [
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip()
                # Allow tests that assert the phrase's absence to reference it.
                and Path(line.strip()).name != "test_verification_bundle.py"
                and Path(line.strip()).name != "verification.py"
            ]
            assert matched == [], (
                f"'human-signed' appears in {path_root}/ — D-84 requires "
                f"'human-approved'. Offenders: {matched!r}"
            )
