"""
polylib.py — Shared plumbing for the live Polymarket bot roster.

Everything here routes through the Bullpen CLI (`bullpen ... --output json`)
rather than raw HTTP: Bullpen owns auth (Turnkey signing), wallet selection,
CLOB credentials and order routing, so the bots never touch a private key.

Safety model (load-bearing, do not weaken):
  - `live` defaults to False in live_config.json. Every mutating command goes
    through `execute()`, which refuses unless live=True AND the CLI session is
    authenticated AND the kill switch is not engaged.
  - The kill switch (data/live_halt.json) is engaged automatically by the
    orchestrator on drawdown breach, and manually via `live_cross.py halt`.
  - Every intended-or-executed order is appended to data/live_actions.jsonl
    so there is always an audit trail, dry-run or not.
"""

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "live_config.json"
DATA = ROOT / "data"
HALT_PATH = DATA / "live_halt.json"
ACTIONS_LOG = DATA / "live_actions.jsonl"
COOLDOWN_PATH = DATA / "poly_cooldown.json"

COOLDOWN_HOURS = 24


# ── time ────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s: str):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def parse_feed_ts(s: str):
    """Feed timestamps come as '2026-07-18 16:18:44 UTC' or ISO. Always
    returns a tz-aware UTC datetime (naive would silently become local)."""
    s = str(s or "").replace(" UTC", "")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    ts = parse_iso(s)
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def hours_until(iso_ts: str) -> float | None:
    dt = parse_iso(iso_ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).total_seconds() / 3600


# ── config ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "live": False,
    "kill_switch_drawdown_usd": 25.0,
    "copy": {
        "enabled": True,
        "n_traders": 3,
        "amount_per_trade_usd": 5.0,
        "daily_limit_usd": 30.0,
        "budget_usd": 60.0,
        "max_per_market_usd": 10.0,
        "min_time_to_resolution_h": 4,
        "slippage_pct": 2.0,
        "max_stale_days": 3,
        "min_lifetime_trades": 200,
        "reselect_hours": 24,
        "pause_if_copied_pnl_below_usd": -10.0,
        "pause_min_executions": 8,
        "max_open_paper": 25,
    },
    "arb": {
        "enabled": True,
        "min_edge": 0.02,
        "max_stake_usd": 25.0,
        "fee_bps": 0,
        "min_leg_depth_mult": 3.0,
        "max_legs": 12,
        "scan_events": 40,
    },
    "seller": {
        "enabled": True,
        "max_yes_price": 0.08,
        "max_no_price": 0.97,
        "min_no_price": 0.90,
        "stake_usd": 5.0,
        "max_open": 10,
        "min_hours_to_resolution": 4,
        "max_hours_to_resolution": 72,
        "min_volume_24h": 50000,
        "max_spread": 0.02,
        "min_depth_mult": 5.0,
        "scan_markets": 200,
    },
    "theta": {
        "enabled": True,
        "min_price": 0.90,
        "max_price": 0.97,
        "min_hours_to_resolution": 2,
        "max_hours_to_resolution": 24,
        "min_volume_24h": 50000,
        "max_spread": 0.02,
        "stake_usd": 5.0,
        "max_open": 8,
        "max_per_event": 3,
        "order_expire_hours": 2,
        "scan_markets": 200,
    },
    "whaleflow": {
        "enabled": True,
        "min_trader_pnl_usd": 100000,
        "min_trade_usd": 500,
        "confirm_whales": 2,
        "single_whale_usd": 3000,
        "window_hours": 6,
        "min_price": 0.05,
        "max_price": 0.90,
        "stake_usd": 5.0,
        "max_open": 10,
        "max_hours_to_resolution": 168,
        "slippage": 0.02,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return _merge(DEFAULT_CONFIG, json.loads(CONFIG_PATH.read_text()))
    return dict(DEFAULT_CONFIG)


# ── bullpen CLI wrapper ─────────────────────────────────────────────────────

class BullpenError(RuntimeError):
    def __init__(self, msg: str, code: str = "", payload: dict | None = None):
        super().__init__(msg)
        self.code = code
        self.payload = payload or {}


def _parse_cli_json(text: str):
    """The CLI sometimes prints human warning lines before the JSON body."""
    for opener in ("{", "["):
        idx = text.find(opener)
        if idx != -1:
            try:
                return json.loads(text[idx:])
            except json.JSONDecodeError:
                continue
    return None


def bp(*args: str, timeout: int = 60, retries: int = 2):
    """
    Run `bullpen <args> --output json`, parse, retry transient failures.
    Raises BullpenError on hard failure. Never prompts (mutating commands
    must pass --yes explicitly at the call site).
    """
    cmd = ["bullpen", *args, "--output", "json"]
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_err = BullpenError(f"timeout: {' '.join(args)}", code="LOCAL_TIMEOUT")
            time.sleep(2 * (attempt + 1))
            continue
        data = _parse_cli_json(r.stdout) or _parse_cli_json(r.stderr)
        if isinstance(data, dict) and (data.get("ok") is False or data.get("status") == "error"):
            code = data.get("error_code") or data.get("code") or ""
            err = BullpenError(data.get("error", "bullpen error"), code=code, payload=data)
            if data.get("retryable") and attempt < retries:
                last_err = err
                time.sleep(3 * (attempt + 1))
                continue
            raise err
        if data is None:
            if r.returncode != 0:
                last_err = BullpenError(
                    (r.stderr or r.stdout or "").strip()[:400] or "bullpen failed",
                    code="NONZERO_EXIT")
                time.sleep(2 * (attempt + 1))
                continue
            return {}
        return data
    raise last_err or BullpenError("bullpen failed")


def bp_ok(*args: str, **kw):
    """bp() that returns None instead of raising — for best-effort reads."""
    try:
        return bp(*args, **kw)
    except BullpenError:
        return None


def auth_ok(status: dict) -> bool:
    """True only for a session that can actually sign trades right now.
    `account.logged_in` alone is a lie — it stays true with an expired,
    refresh-rejected token — so trust the token-validity fields first."""
    health = status.get("health") or {}
    acct = status.get("account") or {}
    if health.get("token_valid") is True:
        return True
    if "access_token_valid" in acct:
        return acct["access_token_valid"] is True
    return bool(acct.get("logged_in")) and not acct.get("reauth_required", False)


def logged_in() -> bool:
    data = bp_ok("status", "--no-split-brain-warning", timeout=30)
    return isinstance(data, dict) and auth_ok(data)


# ── kill switch ─────────────────────────────────────────────────────────────

def halted() -> dict | None:
    if HALT_PATH.exists():
        return json.loads(HALT_PATH.read_text())
    return None


def set_halt(reason: str):
    HALT_PATH.write_text(json.dumps({"halted_at": now_iso(), "reason": reason}, indent=2))


def clear_halt():
    HALT_PATH.unlink(missing_ok=True)


# ── execution gate ──────────────────────────────────────────────────────────

def log_action(record: dict):
    record = {"ts": now_iso(), **record}
    with ACTIONS_LOG.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def execute(cfg: dict, args: list[str], intent: dict) -> dict:
    """
    The single gate every mutating bullpen command goes through.
    Returns {"executed": bool, "result": ...}. In dry-run the command is
    logged but not run.
    """
    stop = halted()
    if stop:
        log_action({**intent, "executed": False, "blocked": f"halt: {stop['reason']}"})
        return {"executed": False, "blocked": "halt"}
    if not cfg.get("live"):
        log_action({**intent, "executed": False, "dry_run": True, "cmd": args})
        return {"executed": False, "dry_run": True}
    if not logged_in():
        log_action({**intent, "executed": False, "blocked": "not logged in"})
        return {"executed": False, "blocked": "auth"}
    result = bp(*args)
    log_action({**intent, "executed": True, "cmd": args, "result": result})
    return {"executed": True, "result": result}


# ── journals (same record shape as botlib, Polymarket venue) ────────────────

def load_journal(path: Path, strategy: str, initial_bankroll: float) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {
        "strategy": strategy,
        "venue": "polymarket",
        "started": now_iso(),
        "initial_bankroll": initial_bankroll,
        "bankroll": initial_bankroll,
        "trades": [],
    }


def save_journal(data: dict, path: Path):
    path.write_text(json.dumps(data, indent=2, default=str))


def open_trades(data: dict) -> list[dict]:
    return [t for t in data.get("trades", []) if t.get("status") == "open"]


def new_trade(slug: str, title: str, outcome: str, shares: float,
              entry_price: float, strategy: str, *, fee: float = 0.0,
              **extra) -> dict:
    cost = round(shares * entry_price + fee, 4)
    trade = {
        "logged_at": now_iso(),
        "strategy": strategy,
        "slug": slug,
        "title": title,
        "outcome": outcome,
        "shares": round(shares, 2),
        "entry_price": round(entry_price, 4),
        "fee": round(fee, 4),
        "cost": cost,
        "status": "open",
        "pnl": None,
        "settled_at": None,
        "exit_reason": None,
    }
    trade.update(extra)
    return trade


# ── cooldown ────────────────────────────────────────────────────────────────

def _load_cooldowns() -> dict:
    if COOLDOWN_PATH.exists():
        return json.loads(COOLDOWN_PATH.read_text())
    return {}


def in_cooldown(slug: str) -> bool:
    ts = _load_cooldowns().get(slug)
    if not ts:
        return False
    placed = parse_iso(ts)
    if placed is None:
        return False
    return (datetime.now(timezone.utc) - placed).total_seconds() < COOLDOWN_HOURS * 3600


def start_cooldown(slug: str):
    cds = _load_cooldowns()
    cds[slug] = now_iso()
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    cds = {k: v for k, v in cds.items()
           if (parse_iso(v) or datetime.now(timezone.utc)).timestamp() > cutoff}
    COOLDOWN_PATH.write_text(json.dumps(cds, indent=2))


# ── market data helpers ─────────────────────────────────────────────────────

def normalize_market(m: dict, event: dict | None = None) -> dict:
    """
    Bullpen surfaces two market shapes: discover/price return a normalized
    snake_case form (outcomes as [{name, price}]); markets/events/event
    return raw Gamma records (camelCase, outcomes as a JSON string,
    numbers stringified). Fold both into the discover shape.
    """
    ev = event or {}
    if isinstance(m.get("outcomes"), list):
        m.setdefault("event_slug", ev.get("slug"))
        return m
    try:
        names = json.loads(m.get("outcomes") or "[]")
        prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
    except (json.JSONDecodeError, TypeError, ValueError):
        names, prices = [], []
    prices += [None] * (len(names) - len(prices))
    own_events = m.get("events") or [{}]
    neg = m.get("enable_neg_risk")
    if neg is None:
        neg = m.get("negRisk")
    if neg is None:
        neg = ev.get("enableNegRisk") or ev.get("enable_neg_risk")
    return {
        "slug": m.get("slug"),
        "question": m.get("question"),
        "outcomes": [{"name": n, "price": p} for n, p in zip(names, prices)],
        "volume_24h": float(m.get("volume24hr") or m.get("volume_24h") or 0),
        "end_date": m.get("endDate") or m.get("endDateIso"),
        "event_slug": (own_events[0] or {}).get("slug") or ev.get("slug"),
        "enable_neg_risk": bool(neg),
        "active": m.get("active", True),
        "closed": m.get("closed", False),
        "enable_order_book": m.get("enableOrderBook",
                                   m.get("enable_order_book", True)),
    }



def fetch_orderbook(market: str, outcome: str | None = None) -> dict | None:
    args = ["polymarket", "orderbook", market]
    if outcome:
        args += ["--outcome", outcome]
    book = bp_ok(*args, timeout=30)
    if not isinstance(book, dict) or "best_bid" not in book:
        return None
    return book


def best_ask_with_depth(book: dict) -> tuple[float, float] | None:
    """(price, size) of the best ask, or None if the ask side is empty."""
    asks = book.get("asks") or []
    if not asks:
        return None
    top = min(asks, key=lambda a: a["price"])
    return float(top["price"]), float(top["size"])
