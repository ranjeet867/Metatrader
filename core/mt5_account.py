"""
mt5_account.py — bridge-backed MT5 account + symbol metadata client.

Cached with TTL=10s to avoid hammering the bridge from the dashboard's
30s-refresh sidebar. force_refresh=True drops the cache and re-queries.

The bridge is the SAME file-bridge protocol used by core/data.py for
copy_rates — methods 'account_info' and 'symbol_info' are added here. If
the bridge doesn't support these (older EA versions), we surface a
RuntimeError so the dashboard can show a degraded state.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from core.data import DEFAULT_BRIDGE_DIR


@dataclass
class AccountInfo:
    login: int
    balance: float
    equity: float
    currency: str
    leverage: int


@dataclass
class SymbolInfo:
    name: str
    tick_size: float
    tick_value: float
    volume_step: float
    volume_min: float
    contract_size: float


# Type alias for the low-level bridge call. Callers can substitute a mock.
BridgeCallFn = Callable[[str, dict], dict]


def _default_bridge_call(method: str, params: dict, *,
                          bridge_dir: Path = DEFAULT_BRIDGE_DIR,
                          timeout_s: float = 10.0) -> dict:
    """Default file-bridge call. Same protocol as core.data.fetch_from_bridge."""
    bridge_dir = Path(bridge_dir)
    req_dir = bridge_dir / "req"
    rep_dir = bridge_dir / "rep"
    req_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)

    rid = uuid.uuid4().hex
    req = {"id": rid, "method": method, "params": params}
    req_path = req_dir / f"{rid}.json"
    rep_path = rep_dir / f"{rid}.json"

    tmp = req_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(req, separators=(",", ":")), encoding="utf-8")
    tmp.rename(req_path)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if rep_path.exists():
            try:
                body = rep_path.read_text(encoding="utf-8", errors="replace").strip()
                if not body:
                    time.sleep(0.05)
                    continue
                resp = json.loads(body)
                rep_path.unlink(missing_ok=True)
                req_path.unlink(missing_ok=True)
                return resp
            except (json.JSONDecodeError, FileNotFoundError):
                time.sleep(0.05)
        time.sleep(0.05)
    raise TimeoutError(f"bridge did not respond to {method} within {timeout_s}s")


class MT5AccountClient:
    def __init__(self, *, bridge_call: Optional[BridgeCallFn] = None,
                 ttl_seconds: float = 10.0):
        self._call = bridge_call or _default_bridge_call
        self.ttl_seconds = float(ttl_seconds)
        self._account_cache: tuple[float, AccountInfo] | None = None
        self._symbol_cache: dict[str, tuple[float, SymbolInfo]] = {}

    def account_info(self, *, force_refresh: bool = False) -> AccountInfo:
        now = time.time()
        if (not force_refresh and self._account_cache is not None
            and now - self._account_cache[0] < self.ttl_seconds):
            return self._account_cache[1]
        resp = self._call("account_info", {})
        if not isinstance(resp, dict) or "ok" in resp and resp.get("ok") is False:
            raise RuntimeError(
                f"bridge returned error for account_info: {resp.get('error', resp)}"
            )
        # Accept either flat shape or nested under "data"
        data = resp.get("data", resp)
        ai = AccountInfo(
            login=int(data.get("login") or data.get("account") or 0),
            balance=float(data.get("balance", 0.0)),
            equity=float(data.get("equity", 0.0)),
            currency=str(data.get("currency", "USD")),
            leverage=int(data.get("leverage", 1)),
        )
        self._account_cache = (now, ai)
        return ai

    def symbol_info(self, name: str, *, force_refresh: bool = False) -> SymbolInfo:
        now = time.time()
        cached = self._symbol_cache.get(name)
        if (not force_refresh and cached is not None
            and now - cached[0] < self.ttl_seconds):
            return cached[1]
        resp = self._call("symbol_info", {"name": name})
        if not isinstance(resp, dict) or resp.get("ok") is False:
            raise RuntimeError(
                f"bridge returned error for symbol_info({name}): "
                f"{resp.get('error', resp) if isinstance(resp, dict) else resp}"
            )
        data = resp.get("data", resp)
        si = SymbolInfo(
            name=name,
            tick_size=float(data.get("tick_size", data.get("point", 0.0))),
            tick_value=float(data.get("tick_value", 0.0)),
            volume_step=float(data.get("volume_step", 0.01)),
            volume_min=float(data.get("volume_min", 0.01)),
            contract_size=float(data.get("contract_size", 1.0)),
        )
        self._symbol_cache[name] = (now, si)
        return si

    def clear_cache(self) -> None:
        self._account_cache = None
        self._symbol_cache.clear()
