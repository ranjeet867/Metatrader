"""
backtest_link.py — single source of truth for every "Open in Backtest"
deep-link the dashboards build.

Every page that links to /Backtest (Composer, Composer Deploy dialog,
Strategy Compare, Strategy Library, Replay-Parity, Operations) MUST
import `build_backtest_url` from here. That guarantees:

  • The URL always carries the EdgeStat's `source_config_json` as a
    base64 blob, so a fresh backtest reproduces the catalog row's
    exact metrics — no more "catalog says PF 1.62, fresh run says
    PF 1.12" mismatches caused by default strategy params overriding
    the catalog's stored stop_atr_mult / target_atr_mult.

  • Adding a new URL field (e.g. risk_pct, side, deployment_id) only
    needs a one-line change here, not a hunt through every caller.

Pre-fix every page hand-built the URL slightly differently — only
ticker/tf/strategy were always present. This module made the round-
trip lossless.
"""
from __future__ import annotations

import base64
import urllib.parse

# Default Streamlit dev-server URL. Pages can override `host` if a
# different deployment uses a non-default port. We prefer absolute URLs
# in LinkColumns (Streamlit's clickable table column) and relative URLs
# in inline markdown (so the link respects whatever host the user has).

DEFAULT_HOST = "http://localhost:8502"


def build_backtest_url(edge_stat,
                         *, host: str | None = DEFAULT_HOST,
                         relative: bool = False) -> str:
    """Build a /Backtest URL that reproduces the EdgeStat's metrics.

    Args:
        edge_stat: a core.edge_catalog.EdgeStat instance. We read
            `.ticker`, `.tf`, `.strategy`, `.source_config_json`.
        host: prefix for the URL. Pass None or set `relative=True` to
            get a path-only URL (useful when embedded in inline HTML).
        relative: if True, omit the host and return `/Backtest?...`.

    Returns:
        A fully-qualified or relative URL string.
    """
    prefix = "" if (relative or host is None) else host
    parts = [
        f"ticker={urllib.parse.quote(str(edge_stat.ticker))}",
        f"tf={urllib.parse.quote(str(edge_stat.tf))}",
        f"strategy={urllib.parse.quote(str(edge_stat.strategy))}",
    ]
    cfg_json = getattr(edge_stat, "source_config_json", "") or ""
    if cfg_json:
        cfg_b64 = (
            base64.urlsafe_b64encode(cfg_json.encode("utf-8"))
                  .decode("ascii")
                  .rstrip("=")    # padding restored on decode
        )
        parts.append(f"config={urllib.parse.quote(cfg_b64)}")
    return f"{prefix}/Backtest?" + "&".join(parts)


def build_backtest_url_from_parts(
    *, ticker: str, tf: str, strategy: str,
    source_config_json: str = "",
    host: str | None = DEFAULT_HOST,
    relative: bool = False,
) -> str:
    """Variant for callers that have raw fields, not an EdgeStat
    (e.g. Deployment objects)."""
    class _Shim:
        pass
    shim = _Shim()
    shim.ticker = ticker
    shim.tf = tf
    shim.strategy = strategy
    shim.source_config_json = source_config_json
    return build_backtest_url(shim, host=host, relative=relative)
