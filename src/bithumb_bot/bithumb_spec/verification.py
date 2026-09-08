"""`VERIFICATION.md` + `manifest.json` bundle scaffolder + parser (D-78, D-84).

Five build-time facts (per ``.planning/phases/01-.../01-CONTEXT.md``
§ canonical_refs) that ``bt m1 fetch-spec`` must present for
human-approval before ``bt m1 verify-facts`` will accept:

1. Public trigger WebSocket is v1, private order WebSocket is v2 (D-91/D-92).
2. JWT claim shape — `timestamp` presence (Open Verification Item #1).
3. Legacy `/trade/stop_limit` fee and legacy `Api-Key`/`Api-Nonce`/
   `Api-Sign` auth — deferred in v1 (D-94).
4. `/v1/orders/chance` pagination cursor inclusivity.
5. Per-channel rate-limit numeric values (Open Verification Item #3).

Each fact section is a Markdown block with the D-78 required columns
as an editable checklist. The operator ticks the appropriate `Status:`
box by hand between `bt m1 fetch-spec` and `bt m1 verify-facts`.

D-84 discipline: the phrase used throughout the template is
**human-approved** (a SHA-256 sidecar detects byte changes but is not
a cryptographic authorship signature — do not describe the artifact
with any phrase that implies otherwise).

D-78 rule: unresolved facts block `bt m1 verify-facts` from
succeeding. Contradicted facts are permitted — they mean the human
explicitly reviewed a discrepancy and captured its effect on the
implementation in the "Effect on implementation" cell.
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
    SnapshotValidationError,
    UnresolvedFactError,
)


@dataclass(frozen=True)
class Fact:
    """One build-time fact awaiting human approval per D-78."""

    key: str
    claim: str


FIVE_BUILD_TIME_FACTS: tuple[Fact, ...] = (
    Fact(
        key="ws_v1_public_v2_private_boundary",
        claim=(
            "Public trigger WebSocket is v1 "
            "(`wss://ws-api.bithumb.com/websocket/v1`); private order "
            "WebSocket is v2 — the two are separate adapter configurations "
            "(D-91 / D-92)."
        ),
    ),
    Fact(
        key="jwt_timestamp_claim_shape",
        claim=(
            "Does the `apidocs.bithumb.com` JWT construction spec require "
            "a `timestamp` claim (millisecond integer) in addition to "
            "`access_key` + `nonce` + optional `query_hash` / "
            "`query_hash_alg`? (Open Verification Item #1 — the "
            "`build_jwt` default is `include_timestamp=False` until "
            "confirmed.)"
        ),
    ),
    Fact(
        key="legacy_stop_limit_deferred",
        claim=(
            "Legacy `/trade/stop_limit` endpoint + legacy "
            "`Api-Key`/`Api-Nonce`/`Api-Sign` HMAC auth are excluded from "
            "v1 (D-94). Adding later = material strategy/execution change "
            "under holdout-burn rules."
        ),
    ),
    Fact(
        key="orders_chance_pagination_cursor",
        claim=(
            "Does `/v1/orders/chance` (and adjacent read endpoints) return "
            "a stable pagination cursor whose inclusivity (inclusive vs. "
            "exclusive of the last-seen row) is documented? Record the "
            "observed cursor field name and its inclusivity semantics."
        ),
    ),
    Fact(
        key="per_channel_rate_limit_values",
        claim=(
            "Per-channel rate-limit numeric values for `public_rest`, "
            "`private_rest`, `public_ws` (Open Verification Item #3). "
            "Record the observed `capacity` and `refill_rate` for each; "
            "these replace the sentinel `capacity=1.0, refill_rate=0.5` "
            "in `bithumb_spec.rate_limits`."
        ),
    ),
)


_REQUIRED_FACT_KEYS: frozenset[str] = frozenset(f.key for f in FIVE_BUILD_TIME_FACTS)


_STATUS_LINE_RE = re.compile(
    r"^\s*-\s+\*\*Status:\*\*\s+(?P<status>.+?)\s*$", re.MULTILINE
)
_FACT_HEADING_RE = re.compile(r"^##\s+Fact:\s+`(?P<key>[a-z0-9_]+)`\s*$", re.MULTILINE)


@dataclass
class ParsedVerification:
    """Result of `parse_verification_bundle`."""

    facts: dict[str, str] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)


def write_verification_bundle(
    bundle_dir: Path,
    snapshot_path: Path,
    fixture_paths: list[Path],
    facts: tuple[Fact, ...] = FIVE_BUILD_TIME_FACTS,
) -> tuple[Path, Path]:
    """Write `VERIFICATION.md` + `manifest.json` into `bundle_dir`.

    Args:
        bundle_dir:    Absolute path to the bundle directory (created if
                       it does not exist).
        snapshot_path: Path of the newly-written spec snapshot.
        fixture_paths: Paths of the sanitized fixtures that fed the
                       snapshot. Their sha256 digests go into the
                       manifest.
        facts:         The build-time facts to enumerate. Defaults to
                       :data:`FIVE_BUILD_TIME_FACTS`.

    Returns:
        ``(verification_md_path, manifest_json_path)``.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    md = _render_verification_md(snapshot_path, fixture_paths, facts)
    md_path = bundle_dir / "VERIFICATION.md"
    md_path.write_text(md, encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "snapshot_path": snapshot_path.name,
        "snapshot_sha256": sha256_hex(snapshot_path.read_bytes()),
        "fixture_paths": [
            {"path": str(p.name), "sha256": sha256_hex(p.read_bytes())}
            for p in fixture_paths
            if p.is_file()
        ],
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
        "recorded its consequence in the *Effect on implementation* cell.",
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
    """Parse `VERIFICATION.md` + `manifest.json` into a structured record."""
    md_path = bundle_dir / "VERIFICATION.md"
    manifest_path = bundle_dir / "manifest.json"
    facts: dict[str, str] = {}
    if md_path.is_file():
        facts = _parse_md_statuses(md_path.read_text(encoding="utf-8"))
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return ParsedVerification(facts=facts, manifest=manifest)


def _parse_md_statuses(text: str) -> dict[str, str]:
    """Extract `{fact_key: status}` from the VERIFICATION.md body.

    A ``## Fact: `<key>`` heading starts a section; the first
    ``- **Status:** ...`` line within that section is inspected for
    a checked box.

    Status conventions (matching the scaffold):

    * ``[x] confirmed`` (any case)  → ``"confirmed"``
    * ``[x] contradicted``          → ``"contradicted"``
    * ``[x] unresolved`` OR nothing → ``"unresolved"``
    """
    fact_positions: list[tuple[int, str]] = []
    for m in _FACT_HEADING_RE.finditer(text):
        fact_positions.append((m.start(), m.group("key")))

    out: dict[str, str] = {}
    for i, (start, key) in enumerate(fact_positions):
        end = fact_positions[i + 1][0] if i + 1 < len(fact_positions) else len(text)
        section = text[start:end]
        status_match = _STATUS_LINE_RE.search(section)
        if status_match is None:
            out[key] = "unresolved"
            continue
        line = status_match.group("status").lower()
        # Detect first ticked box.
        checked = re.findall(r"\[x\]\s*(confirmed|contradicted|unresolved)", line)
        out[key] = checked[0] if checked else "unresolved"
    return out


def verify_facts_bundle(bundle_dir: Path) -> None:
    """Refuse if the bundle has any unresolved required fact (D-78).

    Sequence:
      1. `validate(("m1", "verify-facts"))` (D-85 defense in depth).
      2. Parse the bundle.
      3. For each required fact key: refuse if the parsed status is
         ``"unresolved"`` (or missing).
      4. Recompute the manifest's ``snapshot_sha256`` and compare to
         the current on-disk snapshot. Mismatch → refuse.

    Args:
        bundle_dir: The verification bundle directory (contains
                    `VERIFICATION.md` + `manifest.json`).

    Raises:
        UnresolvedFactError:      one or more required facts still
                                  ``unresolved``.
        SnapshotValidationError:  the manifest's snapshot_sha256 does
                                  not match the on-disk snapshot.
    """
    _result = validate(("m1", "verify-facts"))
    if not _result.ok:
        raise SnapshotValidationError(
            f"validate() refused m1 verify-facts: {_result.reason} "
            f"(missing: {list(_result.missing)!r})"
        )
    parsed = parse_verification_bundle(bundle_dir)
    for key in _REQUIRED_FACT_KEYS:
        status = parsed.facts.get(key, "unresolved")
        if status == "unresolved":
            raise UnresolvedFactError(key)
    # Manifest integrity: snapshot on disk must still match the
    # sha256 the bundle attests.
    if not parsed.manifest:
        raise SnapshotValidationError(
            f"manifest.json missing or empty in bundle {bundle_dir!r}"
        )
    snap_name = parsed.manifest.get("snapshot_path")
    snap_hex = parsed.manifest.get("snapshot_sha256")
    if not isinstance(snap_name, str) or not isinstance(snap_hex, str):
        raise SnapshotValidationError(
            f"manifest.json missing snapshot_path / snapshot_sha256 "
            f"in bundle {bundle_dir!r}"
        )
    # The snapshot may live outside the bundle dir; the manifest
    # records only the filename by convention. Prefer sibling
    # resolution first, else fall back to the recorded name in the
    # bundle dir.
    snap_path = bundle_dir / snap_name
    if not snap_path.is_file():
        raise SnapshotValidationError(
            f"snapshot {snap_name!r} not found next to manifest at "
            f"{bundle_dir!r}"
        )
    on_disk_hex = sha256_hex(snap_path.read_bytes())
    if on_disk_hex != snap_hex:
        raise SnapshotValidationError(
            f"manifest snapshot_sha256 {snap_hex!r} does not match "
            f"on-disk hash {on_disk_hex!r} for {snap_path!r}"
        )


__all__ = [
    "FIVE_BUILD_TIME_FACTS",
    "Fact",
    "ParsedVerification",
    "parse_verification_bundle",
    "verify_facts_bundle",
    "write_verification_bundle",
]
