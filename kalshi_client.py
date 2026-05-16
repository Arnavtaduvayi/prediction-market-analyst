"""
Authenticated Kalshi API client using RSA-PSS signatures.
Loads credentials from .env (key ID) and keys/kalshi_private.pem (private key).
"""

import base64
import os
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KEYS_DIR = Path(__file__).parent / "keys"


def _load_private_key():
    pem_path = KEYS_DIR / "kalshi_private.pem"
    if not pem_path.exists():
        raise FileNotFoundError(
            f"Private key not found at {pem_path}\n"
            "Place your downloaded .pem file there."
        )
    with open(pem_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _load_key_id() -> str:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("KALSHI_API_KEY_ID="):
                return line.split("=", 1)[1].strip()
    val = os.getenv("KALSHI_API_KEY_ID", "")
    if not val:
        raise ValueError(
            "KALSHI_API_KEY_ID not found. Add it to .env:\n"
            "KALSHI_API_KEY_ID=your-key-id-uuid-here"
        )
    return val


def _sign(private_key, timestamp_ms: int, method: str, path: str) -> str:
    msg = f"{timestamp_ms}{method.upper()}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


class KalshiClient:
    """Thin authenticated wrapper around the Kalshi REST API."""

    def __init__(self):
        self._key_id = _load_key_id()
        self._private_key = _load_private_key()
        self._session = requests.Session()

    def _auth_headers(self, method: str, path: str) -> dict:
        ts = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": _sign(self._private_key, ts, method, path),
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: dict = None) -> dict:
        r = self._session.get(
            f"{BASE_URL}{path}",
            params=params or {},
            headers=self._auth_headers("GET", path),
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict) -> dict:
        r = self._session.post(
            f"{BASE_URL}{path}",
            json=body,
            headers=self._auth_headers("POST", path),
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    # ── Portfolio helpers ──────────────────────────────────────────────────────

    def balance(self) -> float:
        """Return available cash balance in dollars."""
        data = self.get("/portfolio/balance")
        # balance is returned in cents
        return float(data.get("balance", 0)) / 100

    def positions(self) -> list[dict]:
        return self.get("/portfolio/positions").get("market_positions", [])

    def open_orders(self) -> list[dict]:
        return self.get("/portfolio/orders").get("orders", [])

    def fills(self, ticker: str = None, limit: int = 100) -> list[dict]:
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self.get("/portfolio/fills", params).get("fills", [])

    # ── Order management ───────────────────────────────────────────────────────

    def place_limit_order(
        self,
        ticker: str,
        side: str,           # "yes" or "no"
        count: int,          # number of contracts (each worth $1 at expiry)
        limit_price: float,  # dollars, e.g. 0.52
        *,
        expiration_ts: int = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Place a limit order. Returns the order dict.
        side="yes" + limit_price=0.52 means: buy YES at max $0.52/contract.
        """
        if dry_run:
            return {
                "dry_run": True,
                "ticker": ticker,
                "side": side,
                "count": count,
                "limit_price": limit_price,
            }
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": count,
            "yes_price": int(round(limit_price * 100)),   # API expects cents integer
        }
        if expiration_ts:
            body["expiration_ts"] = expiration_ts
        return self.post("/portfolio/orders", body)

    def cancel_order(self, order_id: str) -> dict:
        return self.post(f"/portfolio/orders/{order_id}/cancel", {})

    # ── Market data ───────────────────────────────────────────────────────────

    def market(self, ticker: str) -> dict:
        return self.get(f"/markets/{ticker}").get("market", {})

    def markets(self, **params) -> list[dict]:
        return self.get("/markets", params).get("markets", [])

    def trades(self, ticker: str, limit: int = 100) -> list[dict]:
        return self.get("/markets/trades", {"ticker": ticker, "limit": limit}).get("trades", [])
