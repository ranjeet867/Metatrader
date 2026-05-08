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
from datetime import datetime
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
    # New (Phase 2.5): broker context. Older bridges that don't return these
    # leave them empty/0 — every consumer handles the empty case.
    name: str = ""              # account holder
    server: str = ""            # broker server, e.g. "FTMO-Demo", "FTMO-Server2"
    company: str = ""           # broker company, e.g. "FTMO Trader s.r.o."
    trade_mode: int = 0         # MT5: 0=DEMO, 1=CONTEST, 2=REAL
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 0.0

    @property
    def trade_mode_label(self) -> str:
        return {0: "demo", 1: "contest", 2: "real"}.get(self.trade_mode, "demo")


@dataclass
class SymbolInfo:
    name: str
    tick_size: float
    tick_value: float
    volume_step: float
    volume_min: float
    contract_size: float


# ---------------------------------------------------------------------------
# Phase 2: live position + history shapes
# ---------------------------------------------------------------------------

@dataclass
class BridgePosition:
    """Wire shape for a position fetched from the MT5 bridge.
    Fields match what MT5 SymbolInfoXxx returns (subset).
    """
    ticket: int
    symbol: str
    type: int                         # 0=BUY, 1=SELL (MT5 convention)
    volume: float
    price_open: float
    sl: float
    tp: float
    price_current: float
    profit: float
    swap: float
    commission: float
    time_open_utc: str
    magic: int
    comment: str


@dataclass
class BridgeDeal:
    """One historical deal — what came back from history_deals_get."""
    ticket: int
    order: int
    position_id: int
    time_utc: str
    type: int
    entry: int                        # 0=in, 1=out, etc.
    symbol: str
    volume: float
    price: float
    profit: float
    swap: float
    commission: float
    comment: str


@dataclass
class CloseOrderResult:
    ok: bool
    retcode: int
    deal: int
    price: float
    comment: str


@dataclass
class OrderSendResult:
    """Result of a market order send. retcode == 10009 on the bridge
    means TRADE_RETCODE_DONE (success). Anything else is a broker-side
    rejection; check `comment` and `error` for the human reason."""
    ok: bool
    ticket: int
    retcode: int
    deal: int
    fill_price: float
    volume: float
    comment: str
    error: str = ""


class BridgeError(RuntimeError):
    """Raised when the bridge returns a malformed or error response.

    INVARIANT-5: NEVER silently default. The caller may catch BridgeError
    and surface a typed UI message, but we never fabricate data.
    """


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
            name=str(data.get("name", "")),
            server=str(data.get("server", "")),
            company=str(data.get("company", "")),
            trade_mode=int(data.get("trade_mode", 0) or 0),
            margin=float(data.get("margin", 0.0)),
            margin_free=float(data.get("margin_free", 0.0)),
            margin_level=float(data.get("margin_level", 0.0)),
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
        # Two EA generations are in the wild:
        #   • V2Bridge.mq5         emits "tick_value" / "tick_size" / "contract_size"
        #   • MT5BridgeFile.mq5    emits "trade_tick_value" / "trade_tick_size" /
        #                                 "trade_contract_size"
        # Accept both so the python client does not silently end up with 0.0.
        def _pick(*keys, default):
            for k in keys:
                v = data.get(k)
                if v is not None:
                    return v
            return default

        tick_size = _pick("tick_size", "trade_tick_size", "point", default=0.0)
        tick_value = _pick("tick_value", "trade_tick_value", default=0.0)
        contract_size = _pick("contract_size", "trade_contract_size", default=1.0)
        si = SymbolInfo(
            name=name,
            tick_size=float(tick_size),
            tick_value=float(tick_value),
            volume_step=float(data.get("volume_step", 0.01)),
            volume_min=float(data.get("volume_min", 0.01)),
            contract_size=float(contract_size),
        )
        self._symbol_cache[name] = (now, si)
        return si

    def clear_cache(self) -> None:
        self._account_cache = None
        self._symbol_cache.clear()

    # -------- Phase 2: position + history methods --------

    def positions_get(self) -> list[BridgePosition]:
        """All open positions on the connected account.

        Bridge MUST return a list (or {ok:True, data:[...]}). Anything else
        raises BridgeError so the UI can surface a typed reason. NEVER
        silently returns [] on error (INVARIANT-5).
        """
        resp = self._call("positions_get", {})
        rows = self._extract_list(resp, "positions_get")
        out: list[BridgePosition] = []
        for r in rows:
            try:
                out.append(BridgePosition(
                    ticket=int(r["ticket"]),
                    symbol=str(r["symbol"]),
                    type=int(r["type"]),
                    volume=float(r["volume"]),
                    price_open=float(r["price_open"]),
                    sl=float(r.get("sl", 0.0)),
                    tp=float(r.get("tp", 0.0)),
                    price_current=float(r.get("price_current", r["price_open"])),
                    profit=float(r.get("profit", 0.0)),
                    swap=float(r.get("swap", 0.0)),
                    commission=float(r.get("commission", 0.0)),
                    time_open_utc=str(r.get("time_open_utc", r.get("time", ""))),
                    magic=int(r.get("magic", 0)),
                    comment=str(r.get("comment", "")),
                ))
            except (KeyError, ValueError, TypeError) as e:
                raise BridgeError(
                    f"positions_get: malformed row (missing/invalid {e!r}): {r}"
                )
        return out

    def order_send(self, *, symbol: str, direction: str, lots: float,
                    sl: float = 0.0, tp: float = 0.0,
                    deviation: int = 20, comment: str = "",
                    magic: int = 770070) -> OrderSendResult:
        """Send a market order to the broker. Returns OrderSendResult.

        `direction` MUST be "LONG" or "SHORT" — the EA flips it to BUY/SELL.
        SL and TP are stored on the broker (server-side) so they survive
        a Python crash or laptop loss. SL/TP=0.0 means none (not advised
        for live trading).

        Raises BridgeError on connection / shape problems. A broker-side
        rejection (e.g. market closed, bad volume) returns ok=False with
        retcode + error populated — does NOT raise.
        """
        if direction.upper() not in ("LONG", "SHORT"):
            raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")
        if lots <= 0:
            raise ValueError(f"lots must be > 0, got {lots}")
        params = {
            "symbol": symbol,
            "direction": direction.upper(),
            "lots": float(lots),
            "sl": float(sl),
            "tp": float(tp),
            "deviation": int(deviation),
            "comment": comment,
            "magic": int(magic),
        }
        resp = self._call("order_send", params)
        if not isinstance(resp, dict):
            raise BridgeError(f"order_send: expected dict; got {type(resp)}")
        d = resp.get("data", resp)
        # Bridge wraps as {ok: bool, ...} — accept either flat or nested
        ok = bool(d.get("ok", resp.get("ok", False)))
        err = str(resp.get("error", d.get("error", "")) or "")
        if not ok and not err:
            err = f"retcode={d.get('retcode', 'unknown')}"
        return OrderSendResult(
            ok=ok,
            ticket=int(d.get("ticket", 0) or 0),
            retcode=int(d.get("retcode", 0) or 0),
            deal=int(d.get("deal", 0) or 0),
            fill_price=float(d.get("fill_price", d.get("price", 0.0)) or 0.0),
            volume=float(d.get("volume", 0.0) or 0.0),
            comment=str(d.get("comment", "")),
            error=err,
        )

    def position_close(self, ticket: int, deviation: int = 20
                        ) -> CloseOrderResult:
        """Close one position by ticket. Raises BridgeError on bad shape."""
        resp = self._call("position_close",
                            {"ticket": int(ticket), "deviation": int(deviation)})
        if not isinstance(resp, dict):
            raise BridgeError(f"position_close: expected dict; got {type(resp)}")
        # Some bridges wrap the result under data; some don't
        d = resp.get("data", resp)
        ok = bool(d.get("ok", resp.get("ok", True)))
        if not ok and "error" in resp:
            raise BridgeError(f"position_close({ticket}) failed: {resp['error']}")
        return CloseOrderResult(
            ok=ok,
            retcode=int(d.get("retcode", 0)),
            deal=int(d.get("deal", 0)),
            price=float(d.get("price", 0.0)),
            comment=str(d.get("comment", "")),
        )

    def history_deals_get(self, since_utc: datetime,
                            until_utc: datetime | None = None
                            ) -> list[BridgeDeal]:
        """All deals in the window — including manual closes done in MT5.

        `since_utc` and `until_utc` are tz-aware UTC datetimes. The bridge
        is expected to convert to its preferred wire format.
        """
        params = {"since_utc": since_utc.isoformat()}
        if until_utc is not None:
            params["until_utc"] = until_utc.isoformat()
        resp = self._call("history_deals_get", params)
        rows = self._extract_list(resp, "history_deals_get")
        out: list[BridgeDeal] = []
        for r in rows:
            try:
                out.append(BridgeDeal(
                    ticket=int(r["ticket"]),
                    order=int(r.get("order", 0)),
                    position_id=int(r.get("position_id", 0)),
                    time_utc=str(r.get("time_utc", r.get("time", ""))),
                    type=int(r.get("type", 0)),
                    entry=int(r.get("entry", 0)),
                    symbol=str(r["symbol"]),
                    volume=float(r["volume"]),
                    price=float(r["price"]),
                    profit=float(r.get("profit", 0.0)),
                    swap=float(r.get("swap", 0.0)),
                    commission=float(r.get("commission", 0.0)),
                    comment=str(r.get("comment", "")),
                ))
            except (KeyError, ValueError, TypeError) as e:
                raise BridgeError(
                    f"history_deals_get: malformed row "
                    f"(missing/invalid {e!r}): {r}"
                )
        return out

    @staticmethod
    def _extract_list(resp, method_name: str) -> list[dict]:
        """Coerce {ok, data:[...]}, {data:[...]}, or [...] → list of dicts."""
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            if "data" in resp and isinstance(resp["data"], list):
                if resp.get("ok") is False:
                    raise BridgeError(
                        f"{method_name}: bridge returned ok=False: "
                        f"{resp.get('error', '<no error msg>')}"
                    )
                return resp["data"]
            if resp.get("ok") is False:
                raise BridgeError(
                    f"{method_name}: bridge returned ok=False: "
                    f"{resp.get('error', '<no error msg>')}"
                )
        raise BridgeError(
            f"{method_name}: unexpected response shape: {type(resp).__name__}"
        )
