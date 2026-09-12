"""Adversarial tests for D2 (D-erv fix #2): resume-time immutable-prefix
verification via per-candle SHA-256 fingerprints.

Covers must_have truths #4 and #5 from
``.planning/quick/260912-erv-fix-two-correctness-defects-in-the-paper/
260912-erv-PLAN.md``:

* Truth #4 — resuming after mutating ANY byte of ANY previously
  processed forward candle raises
  :class:`~bithumb_bot.errors.ProcessedPrefixMutatedError` BEFORE any
  new line is appended to ``fills.jsonl`` / ``signals.jsonl`` /
  ``candle_fingerprints.jsonl``, naming the offending candle's
  ``open_time_utc`` in the message.
* Truth #5 — resuming after appending ONLY new forward candles (no
  mutation of any prior row) succeeds and advances the cursor.

Also locks the fingerprint's canonical schema (known-answer vector)
and proves D2 catches mutations the looser fills/signals prefix-replay
check (:func:`bithumb_bot.paper.runner._assert_prefix_replay`) would
miss (coincident-signal mutation).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bithumb_bot.artifact.canonical import canonical_bytes, write_with_sidecar
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import ProcessedPrefixMutatedError
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT, run_paper_session
from bithumb_bot.paper.state import candle_fingerprint, load_state

from .conftest import STEP, T0, flat, make_dataset, snapshot_state_dir_hashes

#: Known-answer fixture candle — computed once via a REPL run of
#: `candle_fingerprint` over this EXACT candle; any future accidental
#: change to the fingerprint dict's key set, field order, or
#: str-conversion path breaks this test (locking the canonical schema).
_SHA256_HEX_LEN = 64

_KNOWN_ANSWER_HASH = "ac5e1ce3236fa78eb83292726ad621c744693c731ead15fd1e3675653bfd29f6"

#: Several D2 adversarial tests below (truncate/reorder/malformed-second-
#: line) need at least two recorded fingerprint rows to manipulate.
_MIN_ROWS_FOR_MANIPULATION = 2


def _known_answer_candle() -> Candle:
    return Candle(
        market="KRW-BTC",
        unit_minutes=240,
        open_time_utc=T0,
        open="100000000",
        high="101000000",
        low="99000000",
        close="100500000",
        volume="1.5",
        quote_volume="150750000",
    )


def _mutate_candle(dataset: CandleDataset, idx: int, **overrides: Any) -> CandleDataset:
    """Return a NEW dataset where the candle at position ``idx`` has the
    given field(s) overridden via ``model_copy`` (bypasses field
    validation, so callers must pass already-typed ``Money``/``Qty``/
    ``datetime`` values) — every other candle is byte-identical."""
    candles = list(dataset.candles)
    candles[idx] = candles[idx].model_copy(update=overrides)
    return dataset.model_copy(update={"candles": candles})


def _read_state_fills_signals(tmp_path: Path) -> tuple[bytes, bytes, bytes, bytes]:
    signals_path = tmp_path / "signals.jsonl"
    return (
        (tmp_path / "state.json").read_bytes(),
        (tmp_path / "fills.jsonl").read_bytes(),
        (tmp_path / "candle_fingerprints.jsonl").read_bytes(),
        signals_path.read_bytes() if signals_path.is_file() else b"",
    )


class TestFingerprintKnownAnswerVector:
    def test_fixed_candle_hashes_to_known_value(self) -> None:
        assert candle_fingerprint(_known_answer_candle()) == _KNOWN_ANSWER_HASH
        assert len(_KNOWN_ANSWER_HASH) == _SHA256_HEX_LEN


class TestFingerprintOrderIndependent:
    def test_construction_kwarg_order_does_not_affect_fingerprint(self) -> None:
        common: dict[str, Any] = {
            "market": "KRW-BTC",
            "unit_minutes": 240,
            "open_time_utc": T0,
            "open": "100000000",
            "high": "101000000",
            "low": "99000000",
            "close": "100500000",
            "volume": "1.5",
            "quote_volume": "150750000",
        }
        c1 = Candle(**common)
        c2 = Candle(**dict(reversed(list(common.items()))))
        assert candle_fingerprint(c1) == candle_fingerprint(c2)


class TestMutatedFieldFailsClosed:
    """D2 (Truth #4): resuming after ANY single previously-processed
    forward candle's byte-content mutates must raise
    ``ProcessedPrefixMutatedError`` before any new audit-trail line is
    appended.

    Six of the seven canonical fields (``open``, ``high``, ``low``,
    ``close``, ``volume``, ``quote_volume``) are tested by mutating the
    candle ITSELF on the second invocation — contiguity /
    strictly-ascending-order checks never inspect OHLCV values, so this
    is a faithful "stale mirror overwrote a completed candle"
    reproduction.

    ``open_time_utc`` cannot be tested the same way: mutating a single
    candle's ``open_time_utc`` in isolation either breaks the dataset's
    strictly-ascending / contiguous-grid invariant (caught earlier, by
    ``_dataset_shape_refusal``'s ``InternalCandleGapError``) or, if
    compensated across the whole array, shifts ``paper_start_ts_utc`` /
    the warmup hash (caught earlier, by ``_resume_divergence_check``'s
    ``ForwardDatasetDivergenceError``) — both are correct
    defense-in-depth; D2 only needs to be the LAST line of defense. So
    the ``open_time_utc`` case is exercised via the realistic alternate
    threat: the recorded sidecar (``candle_fingerprints.jsonl``) itself
    is corrupted (a stale mirror / manual edit of the audit trail),
    which reaches the exact same
    ``_verify_processed_prefix_fingerprints`` "missing from the current
    dataset" branch.
    """

    @pytest.mark.parametrize(
        "field", ["open", "high", "low", "close", "volume", "quote_volume"]
    )
    def test_mutated_ohlcv_field_fails_closed(
        self,
        tmp_path: Path,
        paper_fixture: tuple[CandleDataset, object, object],
        field: str,
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)
        before = _read_state_fills_signals(tmp_path)

        mutate_idx = WARMUP_CANDLE_COUNT + 1  # second forward candle
        target = dataset.candles[mutate_idx]
        if field == "volume":
            override: dict[str, Any] = {
                "volume": Qty.from_str(str(target.volume.value + Decimal("1")))
            }
        else:
            current_money: Money = getattr(target, field)
            override = {field: Money.from_str(str(current_money.value + Decimal("1")))}
        mutated_dataset = _mutate_candle(dataset, mutate_idx, **override)

        with pytest.raises(
            ProcessedPrefixMutatedError,
            match=re.escape(target.open_time_utc.isoformat()),
        ):
            run_paper_session(mutated_dataset, snapshot, config, tmp_path, now_utc=now)

        assert _read_state_fills_signals(tmp_path) == before

    def test_mutated_open_time_utc_in_sidecar_fails_closed(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)
        before = _read_state_fills_signals(tmp_path)

        fp_path = tmp_path / "candle_fingerprints.jsonl"
        lines = fp_path.read_text(encoding="utf-8").splitlines()
        assert lines, "expected at least one recorded fingerprint"
        last_row = json.loads(lines[-1])
        recorded_open_time = datetime.fromisoformat(last_row["open_time_utc"])
        # Shift by 1 microsecond -- lands on no candle's open_time_utc in
        # the (unchanged) dataset, so this is unambiguously "missing".
        bogus_open_time = recorded_open_time + timedelta(microseconds=1)
        last_row["open_time_utc"] = bogus_open_time.isoformat()
        lines[-1] = json.dumps(last_row, sort_keys=True, separators=(",", ":"))
        fp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(
            ProcessedPrefixMutatedError,
            match=re.escape(bogus_open_time.isoformat()),
        ):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        state_bytes, fills_bytes, _, signals_bytes = before
        after_state, after_fills, _, after_signals = _read_state_fills_signals(tmp_path)
        assert after_state == state_bytes
        assert after_fills == fills_bytes
        assert after_signals == signals_bytes


class TestMissingProcessedCandleFailsClosed:
    def test_dropped_processed_candle_fails_closed(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)
        state_bytes = (tmp_path / "state.json").read_bytes()
        fills_bytes = (tmp_path / "fills.jsonl").read_bytes()

        dropped_open_time = dataset.candles[-1].open_time_utc
        shortened = make_dataset(list(dataset.candles[:-1]))
        now2 = shortened.candles[-1].open_time_utc + STEP

        with pytest.raises(
            ProcessedPrefixMutatedError,
            match=re.escape(dropped_open_time.isoformat()),
        ):
            run_paper_session(shortened, snapshot, config, tmp_path, now_utc=now2)

        assert (tmp_path / "state.json").read_bytes() == state_bytes
        assert (tmp_path / "fills.jsonl").read_bytes() == fills_bytes


class TestAppendOnlyExtensionSucceeds:
    def test_append_extend_with_unchanged_prefix_succeeds(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        r1 = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)
        assert r1.invalid_reason is None

        fp_path = tmp_path / "candle_fingerprints.jsonl"
        fp_lines_1 = fp_path.read_text(encoding="utf-8").splitlines()
        assert len(fp_lines_1) == r1.forward_candle_count

        extended_candles = list(dataset.candles) + [
            flat(len(dataset.candles) + i, "200000000") for i in range(3)
        ]
        extended_dataset = make_dataset(extended_candles)
        now2 = extended_dataset.candles[-1].open_time_utc + STEP

        r2 = run_paper_session(extended_dataset, snapshot, config, tmp_path, now_utc=now2)

        assert r2.invalid_reason is None
        assert r2.resumed is True
        assert r2.forward_candle_count == r1.forward_candle_count + 3

        fp_lines_2 = fp_path.read_text(encoding="utf-8").splitlines()
        assert len(fp_lines_2) == r1.forward_candle_count + 3
        assert fp_lines_2[: len(fp_lines_1)] == fp_lines_1

        state2 = load_state(tmp_path)
        assert state2 is not None
        assert (
            state2.last_processed_open_utc
            == extended_dataset.candles[-1].open_time_utc.isoformat()
        )


class TestCoincidentSignalMutationStillFailsClosed:
    """`volume` never feeds the ``price_over_sma`` signal path (only
    `close` does) -- proves D2 catches a mutation the looser
    fills/signals prefix-replay check
    (:func:`bithumb_bot.paper.runner._assert_prefix_replay`) would miss
    because the derived fill/signal stream is byte-identical either
    way."""

    def test_volume_only_mutation_still_fails_closed(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)
        state_bytes = (tmp_path / "state.json").read_bytes()
        fills_bytes = (tmp_path / "fills.jsonl").read_bytes()

        mutate_idx = WARMUP_CANDLE_COUNT + 1
        target = dataset.candles[mutate_idx]
        mutated_dataset = _mutate_candle(
            dataset,
            mutate_idx,
            volume=Qty.from_str(str(target.volume.value + Decimal("1"))),
        )

        with pytest.raises(
            ProcessedPrefixMutatedError,
            match=re.escape(target.open_time_utc.isoformat()),
        ):
            run_paper_session(mutated_dataset, snapshot, config, tmp_path, now_utc=now)

        assert (tmp_path / "state.json").read_bytes() == state_bytes
        assert (tmp_path / "fills.jsonl").read_bytes() == fills_bytes


class TestTightenedPrefixIntegrity:
    """D-no0 D2 tightening: five additional corruption modes (missing-
    file, malformed, row-count, order+content, tail), each enforced
    fail-closed BEFORE any file mutation on the resumed invocation. Every
    test captures the state-dir's per-file SHA-256 hash-map AFTER
    injecting its corruption but BEFORE the failing resume, then
    re-asserts byte-equality after ``pytest.raises`` — proving the
    FAILED resume itself did not further mutate anything."""

    def test_row_count_mismatch_fails_closed_truncated(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        fp_path = tmp_path / "candle_fingerprints.jsonl"
        lines = fp_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= _MIN_ROWS_FOR_MANIPULATION
        fp_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        mutated_before = snapshot_state_dir_hashes(tmp_path)

        with pytest.raises(
            ProcessedPrefixMutatedError,
            match=r"has (\d+) rows but state\.json reports forward_candle_count=(\d+)",
        ):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert snapshot_state_dir_hashes(tmp_path) == mutated_before

    def test_row_count_mismatch_fails_closed_extended(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        fp_path = tmp_path / "candle_fingerprints.jsonl"
        lines = fp_path.read_text(encoding="utf-8").splitlines()
        assert lines
        fp_path.write_text("\n".join([*lines, lines[-1]]) + "\n", encoding="utf-8")
        mutated_before = snapshot_state_dir_hashes(tmp_path)

        with pytest.raises(
            ProcessedPrefixMutatedError,
            match=r"has (\d+) rows but state\.json reports forward_candle_count=(\d+)",
        ):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert snapshot_state_dir_hashes(tmp_path) == mutated_before

    def test_order_mismatch_fails_closed(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        fp_path = tmp_path / "candle_fingerprints.jsonl"
        lines = fp_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= _MIN_ROWS_FOR_MANIPULATION
        lines[0], lines[1] = lines[1], lines[0]
        fp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        mutated_before = snapshot_state_dir_hashes(tmp_path)

        with pytest.raises(
            ProcessedPrefixMutatedError,
            match=r"row 0 open_time_utc=.* != current dataset candle\[\d+\]\.open_time_utc=",
        ):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert snapshot_state_dir_hashes(tmp_path) == mutated_before

    def test_tail_mismatch_fails_closed(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        # Recorded rows match the CURRENT dataset's forward candles in
        # order AND content (so the order+content check passes) -- but
        # state.json's last_processed_open_utc disagrees with the last
        # recorded row. This is impossible under normal operation (both
        # are written from the same source); reproduce it by doctoring
        # state.json (+ regenerating its sidecar so SidecarHashMismatchError
        # does NOT fire first) to declare a last_processed_open_utc one
        # step EARLIER than the true last recorded row's open_time.
        state_path = tmp_path / "state.json"
        parsed_state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
        true_last = datetime.fromisoformat(parsed_state["last_processed_open_utc"])
        doctored_last = (true_last - STEP).isoformat()
        parsed_state["last_processed_open_utc"] = doctored_last
        write_with_sidecar(state_path, canonical_bytes(parsed_state))
        mutated_before = snapshot_state_dir_hashes(tmp_path)

        with pytest.raises(
            ProcessedPrefixMutatedError,
            match=re.escape(f"!= state.json last_processed_open_utc={doctored_last}"),
        ):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert snapshot_state_dir_hashes(tmp_path) == mutated_before

    def test_malformed_row_non_json_fails_closed(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        fp_path = tmp_path / "candle_fingerprints.jsonl"
        lines = fp_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= _MIN_ROWS_FOR_MANIPULATION
        lines[1] = "this is not json"
        fp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        mutated_before = snapshot_state_dir_hashes(tmp_path)

        with pytest.raises(ProcessedPrefixMutatedError, match=r"line 2 is malformed"):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert snapshot_state_dir_hashes(tmp_path) == mutated_before

    def test_malformed_row_missing_key_fails_closed(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        fp_path = tmp_path / "candle_fingerprints.jsonl"
        lines = fp_path.read_text(encoding="utf-8").splitlines()
        assert lines
        row = json.loads(lines[0])
        del row["sha256"]
        lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        fp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        mutated_before = snapshot_state_dir_hashes(tmp_path)

        with pytest.raises(ProcessedPrefixMutatedError, match=r"line \d+ is malformed"):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert snapshot_state_dir_hashes(tmp_path) == mutated_before

    def test_malformed_row_bad_sha256_hex_fails_closed(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        fp_path = tmp_path / "candle_fingerprints.jsonl"
        lines = fp_path.read_text(encoding="utf-8").splitlines()
        assert lines
        row = json.loads(lines[0])
        row["sha256"] = "not-a-hex-digest"
        lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        fp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        mutated_before = snapshot_state_dir_hashes(tmp_path)

        with pytest.raises(ProcessedPrefixMutatedError, match=r"line \d+ is malformed"):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert snapshot_state_dir_hashes(tmp_path) == mutated_before

    def test_missing_fingerprint_file_fails_closed(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP
        run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        fp_path = tmp_path / "candle_fingerprints.jsonl"
        assert fp_path.is_file()
        fp_path.unlink()
        mutated_before = snapshot_state_dir_hashes(tmp_path)
        assert mutated_before["candle_fingerprints.jsonl"] is None

        with pytest.raises(ProcessedPrefixMutatedError) as exc_info:
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        message = str(exc_info.value)
        assert re.search(r"candle_fingerprints\.jsonl is missing", message)
        assert re.search(r"forward_candle_count=\d+", message)

        assert snapshot_state_dir_hashes(tmp_path) == mutated_before
