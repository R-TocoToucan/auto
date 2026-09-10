"""Tests for :mod:`bithumb_bot.bithumb_spec.verification` (D-78, D-84, Batch 2)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bithumb_bot.artifact.canonical import (
    canonical_bytes,
    sha256_hex,
    write_with_sidecar,
)
from bithumb_bot.bithumb_spec.verification import (
    FIVE_BUILD_TIME_FACTS,
    MANIFEST_SCHEMA_VERSION,
    parse_verification_bundle,
    verify_facts_bundle,
    write_verification_bundle,
)
from bithumb_bot.config.validator import REPO_ROOT_ENV
from bithumb_bot.errors import (
    ObsoleteVerificationBundleError,
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


def _write_snapshot(bundle_dir: Path, name: str = "snap.json") -> Path:
    """Write a snapshot with sidecar inside `bundle_dir`."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    snap = bundle_dir / name
    write_with_sidecar(snap, b'{"ok":true}\n')
    return snap


def _write_fixture(dir_: Path, name: str, payload: bytes = b'{"fixture":1}\n') -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    fx = dir_ / name
    fx.write_bytes(payload)
    return fx


def _all_approved(md_text: str, approver: str = "op-alice") -> str:
    """Tick every confirmed box AND fill every approver field."""
    text = md_text.replace(
        "**Status:** [ ] confirmed  [ ] contradicted  [ ] unresolved",
        "**Status:** [x] confirmed  [ ] contradicted  [ ] unresolved",
    )
    text = text.replace(
        "**User approval status: human-approved by:** ",
        f"**User approval status: human-approved by:** {approver}",
    )
    return text


def _make_valid_bundle(tmp_path: Path) -> Path:
    """Build a fully-valid bundle: snapshot + one fixture + all facts approved."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    snap = _write_snapshot(bundle_dir)
    fx = _write_fixture(tmp_path / "src_fixtures", "fx1.json")
    md_path, _ = write_verification_bundle(
        bundle_dir, snapshot_path=snap, fixture_paths=[fx]
    )
    md_path.write_text(_all_approved(md_path.read_text(encoding="utf-8")), encoding="utf-8")
    return bundle_dir


# ---------------------------------------------------------------------------
# Bundle scaffold shape (Batch 2 fact set + self-contained artifact copy)
# ---------------------------------------------------------------------------


class TestWriteBundle:
    def test_creates_all_expected_files(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        snap = _write_snapshot(bundle_dir)
        fx = _write_fixture(tmp_path / "src", "fx1.json")
        md_path, manifest_path = write_verification_bundle(
            bundle_dir, snapshot_path=snap, fixture_paths=[fx]
        )
        assert md_path.is_file()
        assert manifest_path.is_file()
        assert (bundle_dir / "manifest.json.sha256").is_file()
        assert (bundle_dir / "fx1.json").is_file()
        assert (bundle_dir / "fx1.json.sha256").is_file()

    def test_verification_md_human_approved_discipline(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        snap = _write_snapshot(bundle_dir)
        md_path, _ = write_verification_bundle(
            bundle_dir, snapshot_path=snap, fixture_paths=[]
        )
        text = md_path.read_text(encoding="utf-8")
        assert "human-approved" in text
        assert "human-signed" not in text

    def test_manifest_records_schema_and_hashes(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        snap = _write_snapshot(bundle_dir)
        fx = _write_fixture(tmp_path / "src", "fx1.json")
        _, manifest_path = write_verification_bundle(
            bundle_dir, snapshot_path=snap, fixture_paths=[fx]
        )
        parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert parsed["schema_version"] == MANIFEST_SCHEMA_VERSION
        assert parsed["snapshot_path"] == "snap.json"
        assert len(parsed["snapshot_sha256"]) == 64
        assert parsed["fixture_paths"] == [
            {"path": "fx1.json", "sha256": sha256_hex(fx.read_bytes())}
        ]

    def test_all_five_facts_scaffolded_and_current_keys(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        snap = _write_snapshot(bundle_dir)
        md_path, _ = write_verification_bundle(
            bundle_dir, snapshot_path=snap, fixture_paths=[]
        )
        text = md_path.read_text(encoding="utf-8")
        for fact in FIVE_BUILD_TIME_FACTS:
            assert f"## Fact: `{fact.key}`" in text
        # Retired keys must be absent.
        for retired in (
            "orders_chance_pagination_cursor",
            "ws_v1_public_v2_private_boundary",
            "legacy_stop_limit_deferred",
            "per_channel_rate_limit_values",
            "jwt_timestamp_claim_shape",
        ):
            assert f"## Fact: `{retired}`" not in text

    def test_current_fact_set_covers_m1_m2_surface(self) -> None:
        keys = {f.key for f in FIVE_BUILD_TIME_FACTS}
        assert keys == {
            "jwt_timestamp_and_query_hash_shape",
            "fee_rates_bid_ask_provenance",
            "min_total_bid_ask_krw_units",
            "price_tick_and_quantity_step_provenance",
            "market_order_and_buy_fee_reservation_readiness",
        }


# ---------------------------------------------------------------------------
# Parse (statuses + approvers)
# ---------------------------------------------------------------------------


class TestParseBundle:
    def test_all_approved(self, tmp_path: Path) -> None:
        bundle_dir = _make_valid_bundle(tmp_path)
        parsed = parse_verification_bundle(bundle_dir)
        for fact in FIVE_BUILD_TIME_FACTS:
            assert parsed.facts[fact.key] == "confirmed"
            assert parsed.approvers[fact.key] == "op-alice"

    def test_unchecked_defaults_to_unresolved(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        snap = _write_snapshot(bundle_dir)
        write_verification_bundle(bundle_dir, snapshot_path=snap, fixture_paths=[])
        parsed = parse_verification_bundle(bundle_dir)
        for fact in FIVE_BUILD_TIME_FACTS:
            assert parsed.facts[fact.key] == "unresolved"
            assert parsed.approvers[fact.key] == ""


# ---------------------------------------------------------------------------
# Full artifact-chain verification (Batch 2 §2)
# ---------------------------------------------------------------------------


class TestVerifyFactsBundle:
    def test_fully_valid_bundle_passes(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = _make_valid_bundle(tmp_path)
        verify_facts_bundle(bundle_dir)

    def test_missing_manifest_raises(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        # Hand-craft a fully-approved VERIFICATION.md with no manifest.
        lines: list[str] = []
        for fact in FIVE_BUILD_TIME_FACTS:
            lines.extend(
                [
                    f"## Fact: `{fact.key}`",
                    "",
                    "**Claim.** (test)",
                    "",
                    "- **Status:** [x] confirmed  [ ] contradicted  [ ] unresolved",
                    "- **User approval status: human-approved by:** op",
                    "",
                ]
            )
        (bundle_dir / "VERIFICATION.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)

    def test_tampered_manifest_raises(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = _make_valid_bundle(tmp_path)
        manifest = bundle_dir / "manifest.json"
        # Mutate manifest without updating the sidecar.
        manifest.write_bytes(manifest.read_bytes() + b" ")
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)

    def test_tampered_snapshot_copy_raises(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = _make_valid_bundle(tmp_path)
        snap = bundle_dir / "snap.json"
        snap.write_bytes(b'{"ok":false}\n')
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)

    def test_tampered_fixture_raises(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = _make_valid_bundle(tmp_path)
        (bundle_dir / "fx1.json").write_bytes(b'{"fixture":2}\n')
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)

    def test_missing_fixture_sidecar_raises(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = _make_valid_bundle(tmp_path)
        (bundle_dir / "fx1.json.sha256").unlink()
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)

    def test_path_traversal_fixture_rejected(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = _make_valid_bundle(tmp_path)
        manifest_path = bundle_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["fixture_paths"] = [
            {"path": "../evil.json", "sha256": "0" * 64}
        ]
        payload = canonical_bytes(manifest)
        manifest_path.write_bytes(payload)
        (bundle_dir / "manifest.json.sha256").write_bytes(
            f"{sha256_hex(payload)}  manifest.json\n".encode("utf-8")
        )
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)

    def test_extra_fact_key_rejected(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = _make_valid_bundle(tmp_path)
        md_path = bundle_dir / "VERIFICATION.md"
        text = md_path.read_text(encoding="utf-8") + (
            "\n## Fact: `bonus_fact`\n\n"
            "- **Status:** [x] confirmed  [ ] contradicted  [ ] unresolved\n"
            "- **User approval status: human-approved by:** op\n"
        )
        md_path.write_text(text, encoding="utf-8")
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)

    def test_duplicate_fact_key_rejected(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = _make_valid_bundle(tmp_path)
        md_path = bundle_dir / "VERIFICATION.md"
        text = md_path.read_text(encoding="utf-8") + (
            "\n## Fact: `jwt_timestamp_and_query_hash_shape`\n\n"
            "- **Status:** [x] confirmed  [ ] contradicted  [ ] unresolved\n"
            "- **User approval status: human-approved by:** op\n"
        )
        md_path.write_text(text, encoding="utf-8")
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)

    def test_unresolved_fact_raises_unresolved_fact_error(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        snap = _write_snapshot(bundle_dir)
        md_path, _ = write_verification_bundle(
            bundle_dir, snapshot_path=snap, fixture_paths=[]
        )
        # DO NOT tick any box.
        with pytest.raises(UnresolvedFactError):
            verify_facts_bundle(bundle_dir)

    def test_missing_approver_rejected(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        snap = _write_snapshot(bundle_dir)
        md_path, _ = write_verification_bundle(
            bundle_dir, snapshot_path=snap, fixture_paths=[]
        )
        # Tick confirmed but leave every approver field empty.
        md_path.write_text(
            md_path.read_text(encoding="utf-8").replace(
                "**Status:** [ ] confirmed  [ ] contradicted  [ ] unresolved",
                "**Status:** [x] confirmed  [ ] contradicted  [ ] unresolved",
            ),
            encoding="utf-8",
        )
        with pytest.raises(SnapshotValidationError):
            verify_facts_bundle(bundle_dir)


# ---------------------------------------------------------------------------
# Legacy bundle refusal (Batch 2 §1)
# ---------------------------------------------------------------------------


class TestLegacyBundleObsolete:
    def test_v1_fact_keys_rejected_with_obsolete_error(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        _write_snapshot(bundle_dir)
        # Legacy fact set — includes obsolete keys.
        legacy = [
            "## Fact: `orders_chance_pagination_cursor`",
            "",
            "- **Status:** [x] confirmed  [ ] contradicted  [ ] unresolved",
            "- **User approval status: human-approved by:** op",
            "",
        ]
        (bundle_dir / "VERIFICATION.md").write_text(
            "\n".join(legacy) + "\n", encoding="utf-8"
        )
        with pytest.raises(ObsoleteVerificationBundleError):
            verify_facts_bundle(bundle_dir)

    def test_v1_schema_version_rejected_with_obsolete_error(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        bundle_dir = _make_valid_bundle(tmp_path)
        # Downgrade manifest schema_version to 1 (obsolete) and refresh
        # the sidecar so the manifest-integrity check would otherwise
        # pass — we want the obsolete-schema branch, not the integrity
        # branch, to fire first.
        manifest_path = bundle_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 1
        payload = canonical_bytes(manifest)
        manifest_path.write_bytes(payload)
        (bundle_dir / "manifest.json.sha256").write_bytes(
            f"{sha256_hex(payload)}  manifest.json\n".encode("utf-8")
        )
        with pytest.raises(ObsoleteVerificationBundleError):
            verify_facts_bundle(bundle_dir)


# ---------------------------------------------------------------------------
# Discipline sweep — D-84
# ---------------------------------------------------------------------------


class TestNoHumanSignedInCodebase:
    """T-1-04-06: the phrase 'human-signed' MUST NOT appear in src/ or tests/ code."""

    def test_grep_returns_zero_matches(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent.parent
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
            _ALLOWED = {
                "test_verification_bundle.py",
                "verification.py",
                "test_fetch_spec_end_to_end.py",
                "test_integration_wave3.py",
            }
            matched = [
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip() and Path(line.strip()).name not in _ALLOWED
            ]
            assert matched == [], (
                f"'human-signed' appears in {path_root}/ — D-84 requires "
                f"'human-approved'. Offenders: {matched!r}"
            )
