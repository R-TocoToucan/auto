"""`VERIFICATION.md` + `manifest.json` bundle scaffolder + parser (D-78, D-84).

Five build-time facts scoped to the current M1/M2 surface. Each fact
maps to a specific field the runtime code path consumes; obsolete
facts (v1 pagination, WS boundary, legacy stop-limit, sentinel
rate-limit values) have been retired.

The bundle records ``schema_version=2``; older bundles fail with a
concise ``ObsoleteVerificationBundleError`` rather than asking the
operator to approve an invalid fact set.

D-78 rule: unresolved facts block `bt m1 verify-facts` from
succeeding. Contradicted facts are permitted — they mean the human
explicitly reviewed a discrepancy and captured its effect on the
implementation in the ``Effect on implementation`` cell.

D-84 discipline: the phrase used throughout the template is
**human-approved** (a SHA-256 sidecar detects byte changes but is not
a cryptographic authorship signature — do not describe the artifact
with any phrase that implies otherwise).

Bundle layout (self-contained — no operator-local paths):

    <bundle_dir>/
      VERIFICATION.md
      manifest.json
      manifest.json.sha256
      <snapshot.name>
      <snapshot.name>.sha256
      <fixture-1.name>
      <fixture-1.name>.sha256
      ...

``verify_facts_bundle`` verifies the full chain: manifest bytes vs.
its sidecar, snapshot bytes vs. its sidecar and vs. the manifest,
each fixture vs. its sidecar and vs. the manifest, no duplicates,
no extras, no paths escaping the bundle root.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bithumb_bot.artifact.canonical import (
    canonical_bytes,
    sha256_hex,
    write_with_sidecar,
)
from bithumb_bot.artifact.timestamps import utc_now
from bithumb_bot.config.validator import validate
from bithumb_bot.errors import (
    ObsoleteVerificationBundleError,
    SnapshotValidationError,
    UnresolvedFactError,
)

#: The current bundle schema. Bumped from 1 → 2 when the fact set was
#: aligned to the M1/M2 surface (Batch 2). Bundles carrying a lower
#: number, no number, or the retired v1 fact keys are refused with
#: :class:`ObsoleteVerificationBundleError`.
MANIFEST_SCHEMA_VERSION: int = 2


@dataclass(frozen=True)
class Fact:
    """One build-time fact awaiting human approval per D-78."""

    key: str
    claim: str


FIVE_BUILD_TIME_FACTS: tuple[Fact, ...] = (
    Fact(
        key="jwt_timestamp_and_query_hash_shape",
        claim=(
            "JWT bearer for `/v1/orders/chance` carries `access_key`, "
            "`nonce`, and a millisecond-integer `timestamp` claim; when "
            "query parameters are present, `query_hash` (SHA-512 over "
            "the alphabetized URL-encoded query string) and "
            "`query_hash_alg=\"SHA512\"` are added. Record the source "
            "URL that documents this claim shape and the exact request "
            "that produced the observed 200 response."
        ),
    ),
    Fact(
        key="fee_rates_bid_ask_provenance",
        claim=(
            "Record the observed `bid_fee` and `ask_fee` from the "
            "`/v1/orders/chance` response and the source URL that "
            "documents them as fee rates. State whether the returned "
            "values match the documented rates; a discrepancy is a "
            "`contradicted` outcome, not a resolution."
        ),
    ),
    Fact(
        key="min_total_bid_ask_krw_units",
        claim=(
            "Record `market.bid.min_total` and `market.ask.min_total` "
            "from the response and confirm both are denominated in KRW "
            "(not the base asset). The M1/M2 minimum-order enforcement "
            "compares KRW notionals; a base-asset denomination is a "
            "contradiction that must be recorded."
        ),
    ),
    Fact(
        key="price_tick_and_quantity_step_provenance",
        claim=(
            "Record the price-tick and quantity-step source in the "
            "response (e.g. `market.bid.price_unit` for the tick), the "
            "documentation URL, and any per-price-band structure "
            "observed. If either is not directly present in "
            "`/v1/orders/chance`, mark unresolved — invented values "
            "are refused."
        ),
    ),
    Fact(
        key="market_order_and_buy_fee_reservation_readiness",
        claim=(
            "Record whether the observed `market.order_types` list "
            "confirms the market-buy `price` and market-sell `market` "
            "order types this project would need, AND record the "
            "documented buy-fee-reservation policy (`market_buy_fee_"
            "reservation`). Absence of an order type in this "
            "read-only response is unresolved, not unsupported."
        ),
    ),
)


_REQUIRED_FACT_KEYS: frozenset[str] = frozenset(f.key for f in FIVE_BUILD_TIME_FACTS)

# Fact keys that only appear in obsolete (pre-Batch-2) bundles. Their
# presence in a parsed VERIFICATION.md triggers a clean
# "obsolete schema" refusal instead of a per-fact unresolved refusal.
_OBSOLETE_FACT_KEYS: frozenset[str] = frozenset(
    {
        "ws_v1_public_v2_private_boundary",
        "legacy_stop_limit_deferred",
        "orders_chance_pagination_cursor",
        "per_channel_rate_limit_values",
        # v1 fact whose wording still referenced include_timestamp=False;
        # the v2 replacement uses a different key so a lingering v1
        # fact section is caught by presence of this key.
        "jwt_timestamp_claim_shape",
    }
)


_STATUS_LINE_RE = re.compile(
    r"^\s*-\s+\*\*Status:\*\*\s+(?P<status>.+?)\s*$", re.MULTILINE
)
_FACT_HEADING_RE = re.compile(r"^##\s+Fact:\s+`(?P<key>[a-z0-9_]+)`\s*$", re.MULTILINE)
_APPROVED_BY_RE = re.compile(
    r"^\s*-\s+\*\*User approval status: human-approved by:\*\*\s*(?P<who>.+?)\s*$",
    re.MULTILINE,
)


@dataclass
class ParsedVerification:
    """Result of :func:`parse_verification_bundle`."""

    facts: dict[str, str] = field(default_factory=dict)
    approvers: dict[str, str] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)


def write_verification_bundle(
    bundle_dir: Path,
    snapshot_path: Path,
    fixture_paths: list[Path],
    facts: tuple[Fact, ...] = FIVE_BUILD_TIME_FACTS,
) -> tuple[Path, Path]:
    """Write a self-contained verification bundle into ``bundle_dir``.

    Copies the snapshot (and its sidecar) plus every fixture (with a
    fresh sidecar) into ``bundle_dir`` so ``verify_facts_bundle`` can
    validate the full artifact chain from bundle-local files alone.

    Returns:
        ``(verification_md_path, manifest_json_path)``.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot: ensure a bundle-local copy + sidecar exist.
    bundle_snapshot = bundle_dir / snapshot_path.name
    if snapshot_path.resolve() != bundle_snapshot.resolve():
        bundle_snapshot.write_bytes(snapshot_path.read_bytes())
        sidecar_src = snapshot_path.with_name(snapshot_path.name + ".sha256")
        sidecar_dst = bundle_dir / (snapshot_path.name + ".sha256")
        if sidecar_src.is_file():
            sidecar_dst.write_bytes(sidecar_src.read_bytes())
        elif not sidecar_dst.is_file():
            write_with_sidecar(bundle_snapshot, bundle_snapshot.read_bytes())
    snapshot_hex = sha256_hex(bundle_snapshot.read_bytes())

    # Fixtures: copy each into the bundle with a fresh sidecar.
    fixture_entries: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for fx in fixture_paths:
        if not fx.is_file():
            continue
        if fx.name in seen_names:
            raise SnapshotValidationError(
                f"duplicate fixture filename in bundle: {fx.name!r}"
            )
        seen_names.add(fx.name)
        bundle_fx = bundle_dir / fx.name
        payload = fx.read_bytes()
        if bundle_fx.resolve() != fx.resolve():
            bundle_fx.write_bytes(payload)
        sidecar_dst = bundle_dir / (fx.name + ".sha256")
        sidecar_dst.write_bytes(
            f"{sha256_hex(payload)}  {fx.name}\n".encode("utf-8")
        )
        fixture_entries.append({"path": fx.name, "sha256": sha256_hex(payload)})

    md = _render_verification_md(bundle_snapshot, [Path(e["path"]) for e in fixture_entries], facts)
    md_path = bundle_dir / "VERIFICATION.md"
    md_path.write_text(md, encoding="utf-8")

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_path": bundle_snapshot.name,
        "snapshot_sha256": snapshot_hex,
        "fixture_paths": fixture_entries,
        "retrieved_at_utc": utc_now().isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    }
    manifest_path = bundle_dir / "manifest.json"
    write_with_sidecar(manifest_path, canonical_bytes(manifest))
    return md_path, manifest_path


def _render_verification_md(
    snapshot_path: Path,
    fixture_paths: list[Path],
    facts: tuple[Fact, ...],
) -> str:
    """Render the VERIFICATION.md scaffold — no status pre-checked (D-84)."""
    lines: list[str] = [
        "<!-- D-84: this bundle is **human-approved**. -->",
        "",
        "# Bithumb Spec Verification Bundle",
        "",
        f"- **Snapshot:** `{snapshot_path.name}`",
        f"- **Sanitized fixtures:** {[p.name for p in fixture_paths]}",
        "",
        "**Approval discipline (D-78, D-84):** each fact below MUST reach status",
        "`confirmed` OR `contradicted` before `bt m1 verify-facts` will succeed.",
        "A `contradicted` fact means the operator reviewed the discrepancy and",
        "recorded its consequence in the *Effect on implementation* cell. Every",
        "confirmed/contradicted fact requires the *User approval status: human-",
        "approved by* field to be filled in.",
        "",
    ]
    for fact in facts:
        lines.extend(_render_fact_section(fact))
    return "\n".join(lines) + "\n"


def _render_fact_section(fact: Fact) -> list[str]:
    return [
        f"## Fact: `{fact.key}`",
        "",
        f"**Claim.** {fact.claim}",
        "",
        "- **Status:** [ ] confirmed  [ ] contradicted  [ ] unresolved",
        "- **Documentation URL:** ",
        "- **Access timestamp (UTC):** ",
        "- **Endpoint tested:** ",
        "- **Sanitized fixture path + SHA-256:** ",
        "- **Observed result:** ",
        "- **Effect on implementation:** ",
        "- **Remaining limitation:** ",
        "- **User approval status: human-approved by:** ",
        "",
    ]


def parse_verification_bundle(bundle_dir: Path) -> ParsedVerification:
    """Parse `VERIFICATION.md` + `manifest.json` into a structured record.

    Reports status and approver-name per fact. Manifest bytes are read
    as-is; hash verification is done by :func:`verify_facts_bundle`.
    """
    md_path = bundle_dir / "VERIFICATION.md"
    manifest_path = bundle_dir / "manifest.json"
    facts: dict[str, str] = {}
    approvers: dict[str, str] = {}
    if md_path.is_file():
        text = md_path.read_text(encoding="utf-8")
        facts = _parse_md_statuses(text)
        approvers = _parse_md_approvers(text)
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SnapshotValidationError(
                f"manifest.json is not valid JSON in bundle {bundle_dir!r}: {exc}"
            ) from exc
    return ParsedVerification(facts=facts, approvers=approvers, manifest=manifest)


def _iter_fact_sections(text: str) -> list[tuple[str, str]]:
    positions: list[tuple[int, str]] = [
        (m.start(), m.group("key")) for m in _FACT_HEADING_RE.finditer(text)
    ]
    sections: list[tuple[str, str]] = []
    for i, (start, key) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        sections.append((key, text[start:end]))
    return sections


def _parse_md_statuses(text: str) -> dict[str, str]:
    """Extract `{fact_key: status}` from the VERIFICATION.md body.

    Status conventions (matching the scaffold):

    * ``[x] confirmed`` (any case)  → ``"confirmed"``
    * ``[x] contradicted``          → ``"contradicted"``
    * ``[x] unresolved`` OR nothing → ``"unresolved"``
    """
    out: dict[str, str] = {}
    for key, section in _iter_fact_sections(text):
        status_match = _STATUS_LINE_RE.search(section)
        if status_match is None:
            out[key] = "unresolved"
            continue
        line = status_match.group("status").lower()
        checked = re.findall(r"\[x\]\s*(confirmed|contradicted|unresolved)", line)
        out[key] = checked[0] if checked else "unresolved"
    return out


def _parse_md_approvers(text: str) -> dict[str, str]:
    """Extract `{fact_key: approver_name}` (blank if unfilled)."""
    out: dict[str, str] = {}
    for key, section in _iter_fact_sections(text):
        match = _APPROVED_BY_RE.search(section)
        out[key] = match.group("who").strip() if match else ""
    return out


def _verify_sidecar(target: Path, bundle_dir: Path) -> None:
    """Check that ``target`` and its sidecar are inside ``bundle_dir`` and match."""
    sidecar = target.with_name(target.name + ".sha256")
    resolved_bundle = bundle_dir.resolve()
    for p in (target, sidecar):
        try:
            resolved = p.resolve()
        except OSError as exc:
            raise SnapshotValidationError(
                f"cannot resolve bundle artifact {p.name!r}: {exc}"
            ) from exc
        if not resolved.is_relative_to(resolved_bundle):
            raise SnapshotValidationError(
                f"bundle artifact {p.name!r} escapes bundle root"
            )
    if not target.is_file():
        raise SnapshotValidationError(
            f"bundle artifact missing on disk: {target.name!r}"
        )
    if not sidecar.is_file():
        raise SnapshotValidationError(
            f"bundle sidecar missing on disk: {sidecar.name!r}"
        )
    recorded = sidecar.read_text(encoding="utf-8").strip().split()
    if len(recorded) < 1 or len(recorded[0]) != 64:
        raise SnapshotValidationError(
            f"bundle sidecar malformed: {sidecar.name!r}"
        )
    on_disk = sha256_hex(target.read_bytes())
    if recorded[0] != on_disk:
        raise SnapshotValidationError(
            f"bundle artifact {target.name!r} does not match its sidecar hash"
        )


def _require_bundle_local_name(name: object, kind: str) -> str:
    """Reject empty, non-string, or path-shaped names.

    ``name`` must be a bare filename with no directory separators — the
    manifest is CI/bundle-local, so any embedded ``/`` or ``..`` is
    either a legacy shape or an attempt at path traversal.
    """
    if not isinstance(name, str) or not name:
        raise SnapshotValidationError(
            f"manifest {kind} path must be a non-empty string; got {name!r}"
        )
    if "/" in name or "\\" in name or ".." in name.split("/"):
        raise SnapshotValidationError(
            f"manifest {kind} path escapes bundle root: {name!r}"
        )
    return name


def verify_facts_bundle(bundle_dir: Path) -> None:
    """Refuse if the bundle is incomplete, obsolete, or unresolved (D-78).

    Sequence:

      1. ``validate(("m1", "verify-facts"))`` (D-85 defense in depth).
      2. Parse VERIFICATION.md + manifest.json.
      3. Reject an obsolete schema or an obsolete fact set with
         :class:`ObsoleteVerificationBundleError` (never ask the operator
         to approve invalid facts).
      4. Verify manifest.json vs. manifest.json.sha256.
      5. Require the exact required fact set — reject missing, extra,
         or duplicate keys.
      6. Every fact must have exactly one ticked status; a ticked
         ``confirmed`` or ``contradicted`` must carry a non-empty
         approver name.
      7. Every referenced snapshot / fixture must sit inside
         ``bundle_dir`` (no path traversal) and match its sidecar and
         the manifest's recorded hash.

    Raises:
        ObsoleteVerificationBundleError:
            legacy schema or legacy fact set detected.
        UnresolvedFactError:
            one or more required facts still ``unresolved``.
        SnapshotValidationError:
            manifest integrity, artifact integrity, path safety,
            approval-field, or fact-set completeness check failed.
    """
    _result = validate(("m1", "verify-facts"))
    if not _result.ok:
        raise SnapshotValidationError(
            f"validate() refused m1 verify-facts: {_result.reason} "
            f"(missing: {list(_result.missing)!r})"
        )

    parsed = parse_verification_bundle(bundle_dir)

    # Obsolete-schema / obsolete-fact detection first, so operators see
    # a schema-versioned refusal instead of a per-fact unresolved list.
    parsed_keys = set(parsed.facts.keys())
    if parsed_keys & _OBSOLETE_FACT_KEYS:
        raise ObsoleteVerificationBundleError(bundle_dir)
    manifest_schema = parsed.manifest.get("schema_version")
    if not parsed.manifest:
        raise SnapshotValidationError(
            f"manifest.json missing or empty in bundle {bundle_dir!r}"
        )
    if manifest_schema != MANIFEST_SCHEMA_VERSION:
        raise ObsoleteVerificationBundleError(bundle_dir)

    # Manifest sidecar verification.
    manifest_path = bundle_dir / "manifest.json"
    _verify_sidecar(manifest_path, bundle_dir)

    # Fact-set completeness: no extras, no missing.
    extra = parsed_keys - _REQUIRED_FACT_KEYS
    missing = _REQUIRED_FACT_KEYS - parsed_keys
    if extra or missing:
        raise SnapshotValidationError(
            f"verification fact set mismatch in {bundle_dir!r}: "
            f"missing={sorted(missing)!r}, extra={sorted(extra)!r}"
        )

    # Duplicate heading detection: count `## Fact: `<key>`` occurrences.
    md_text = (bundle_dir / "VERIFICATION.md").read_text(encoding="utf-8")
    heading_counts: dict[str, int] = {}
    for _pos, key in [
        (m.start(), m.group("key")) for m in _FACT_HEADING_RE.finditer(md_text)
    ]:
        heading_counts[key] = heading_counts.get(key, 0) + 1
    duplicates = sorted(k for k, n in heading_counts.items() if n > 1)
    if duplicates:
        raise SnapshotValidationError(
            f"verification fact duplicated in {bundle_dir!r}: {duplicates!r}"
        )

    # Per-fact status + approval-field enforcement.
    for key in sorted(_REQUIRED_FACT_KEYS):
        status = parsed.facts.get(key, "unresolved")
        if status == "unresolved":
            raise UnresolvedFactError(key)
        approver = parsed.approvers.get(key, "").strip()
        if not approver:
            raise SnapshotValidationError(
                f"fact {key!r} is {status!r} but no approver is recorded "
                f"in the 'User approval status: human-approved by' field"
            )

    # Snapshot chain.
    snap_name = _require_bundle_local_name(
        parsed.manifest.get("snapshot_path"), "snapshot"
    )
    snap_hex = parsed.manifest.get("snapshot_sha256")
    if not isinstance(snap_hex, str) or len(snap_hex) != 64:
        raise SnapshotValidationError(
            f"manifest.snapshot_sha256 malformed in bundle {bundle_dir!r}"
        )
    snap_path = bundle_dir / snap_name
    _verify_sidecar(snap_path, bundle_dir)
    on_disk_hex = sha256_hex(snap_path.read_bytes())
    if on_disk_hex != snap_hex:
        raise SnapshotValidationError(
            f"manifest snapshot_sha256 does not match on-disk hash for "
            f"{snap_name!r} in bundle {bundle_dir!r}"
        )

    # Fixture chain.
    fixture_entries = parsed.manifest.get("fixture_paths", [])
    if not isinstance(fixture_entries, list):
        raise SnapshotValidationError(
            f"manifest.fixture_paths must be a list in {bundle_dir!r}"
        )
    seen_names: set[str] = set()
    for entry in fixture_entries:
        if not isinstance(entry, dict):
            raise SnapshotValidationError(
                f"manifest.fixture_paths entry must be an object in {bundle_dir!r}"
            )
        fx_name = _require_bundle_local_name(entry.get("path"), "fixture")
        fx_hex = entry.get("sha256")
        if not isinstance(fx_hex, str) or len(fx_hex) != 64:
            raise SnapshotValidationError(
                f"fixture {fx_name!r} sha256 malformed in {bundle_dir!r}"
            )
        if fx_name in seen_names:
            raise SnapshotValidationError(
                f"duplicate fixture entry in manifest: {fx_name!r}"
            )
        seen_names.add(fx_name)
        fx_path = bundle_dir / fx_name
        _verify_sidecar(fx_path, bundle_dir)
        if sha256_hex(fx_path.read_bytes()) != fx_hex:
            raise SnapshotValidationError(
                f"fixture {fx_name!r} does not match its recorded hash"
            )


__all__ = [
    "FIVE_BUILD_TIME_FACTS",
    "MANIFEST_SCHEMA_VERSION",
    "Fact",
    "ParsedVerification",
    "parse_verification_bundle",
    "verify_facts_bundle",
    "write_verification_bundle",
]
