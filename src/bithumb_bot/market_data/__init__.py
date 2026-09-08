"""Phase-2 KRW-BTC market-data pipeline (library-only).

This package fetches native Bithumb 240-minute candles through the
public REST endpoint, validates them, and persists a deterministic
dataset alongside a SHA-256 sidecar so the same bytes can be replayed
identically by the simulator later.

Scope-locked (`docs/IMPLEMENTATION_SCOPE.md` §1):

* Only the KRW-BTC / 240-minute path.
* No CLI verb, no capability registry entry, no strategy, no simulator.
* Real network fetching is FAIL-CLOSED until the public REST rate limit
  is a frozen M1 verification item — the fetch orchestrator refuses to
  run without an injected ``httpx.AsyncBaseTransport``.
"""

from bithumb_bot.market_data.candles import (
    Candle,
    FetchResult,
    fetch_candles,
    normalize_and_validate_page,
)
from bithumb_bot.market_data.dataset import (
    CandleDataset,
    DatasetProvenance,
    load_dataset,
    write_dataset_with_sidecar,
)

__all__ = [
    "Candle",
    "CandleDataset",
    "DatasetProvenance",
    "FetchResult",
    "fetch_candles",
    "load_dataset",
    "normalize_and_validate_page",
    "write_dataset_with_sidecar",
]
