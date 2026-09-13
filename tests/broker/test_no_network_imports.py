"""Assert the broker package pulls in no network or credential code.

A source-level scan is sufficient (and desirable — a runtime scan of
``sys.modules`` misses conditional imports). This test is the guardrail
that keeps the MockBroker "no HTTP, WebSocket, Bithumb API, credential
or environment access" invariant honest as the package grows.
"""

from __future__ import annotations

from pathlib import Path

import bithumb_bot.broker as broker_pkg

_FORBIDDEN_IMPORT_MODULES = (
    "httpx",
    "websockets",
    "requests",
    "urllib3",
    "urllib.request",
    "aiohttp",
    "grpc",
    "jwt",
    "PyJWT",
    "socket",
    "ssl",
    "bithumb_bot.market_data",
    "bithumb_bot.secrets",
)

_FORBIDDEN_ATTRIBUTES = (
    "os.environ",
    "os.getenv",
    "os.putenv",
)


def test_broker_source_has_no_network_or_credential_imports() -> None:
    pkg_root = Path(broker_pkg.__file__).parent
    for path in sorted(pkg_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for mod in _FORBIDDEN_IMPORT_MODULES:
            assert f"import {mod}" not in source, (
                f"{path} imports forbidden module {mod!r}"
            )
            assert f"from {mod} " not in source, (
                f"{path} imports from forbidden module {mod!r}"
            )
            assert f"from {mod}\n" not in source, (
                f"{path} imports from forbidden module {mod!r}"
            )
        for attr in _FORBIDDEN_ATTRIBUTES:
            assert attr not in source, (
                f"{path} references forbidden attribute {attr!r}"
            )
