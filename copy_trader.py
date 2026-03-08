#!/usr/bin/env python3
"""
Polymarket Copy Trader

Reads suspects.json from the insider tracker, finds actionable trades
from core wallets, and automatically places follow orders via polymarket CLI.

Designed to run as a GitHub Actions step after the tracker scan.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ─── Config ───────────────────────────────────────────────────────

SUSPECTS_FILE = Path(__file__).parent / "suspects.json"
COPY_STATE_FILE = Path(__file__).parent / "copy_state.json"
COPY_LOG_FILE = Path(__file__).parent / "copy_trades.json"

# Risk parameters (overridable via env vars)
MAX_PER_TRADE = float(os.environ.get("COPY_MAX_PER_TRADE", "5"))       # $5 per copy trade
MAX_TOTAL_EXPOSURE = float(os.environ.get("COPY_MAX_EXPOSURE", "20"))  # $20 max total
MIN_WHALE_SIZE = float(os.environ.get("COPY_MIN_WHALE_SIZE", "100000"))  # only copy ≥$100K trades
MIN_PRICE = float(os.environ.get("COPY_MIN_PRICE", "0.05"))           # don't buy below 5¢
MAX_PRICE = float(os.environ.get("COPY_MAX_PRICE", "0.92"))           # don't buy above 92¢
ONLY_BUY = os.environ.get("COPY_ONLY_BUY", "true").lower() == "true"
DRY_RUN = os.environ.get("COPY_DRY_RUN", "false").lower() == "true"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def log(msg: str):
    print(f"[copy_trader] {msg}", file=sys.stderr)


def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()
        urllib.request.urlopen(url, data=payload, timeout=20).read()
    except Exception as e:
        log(f"TG send failed: {e}")


def run_cli(args: List[str], timeout: int = 30) -> Any:
    cmd = ["polymarket", *args, "-o", "json"]
    log(f"CLI: {' '.join(cmd)}")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"CLI failed ({p.returncode}): {p.stderr.strip()}")
    out = p.stdout.strip()
    if not out:
        return None
    return json.loads(out)


def load_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_json(path: Path, data: Any):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def get_market_tokens(slug: str) -> Optional[Dict[str, str]]:
    """Get token IDs for a market slug. Returns {outcome: token_id}."""
    try:
        market = run_cli(["markets", "get", slug])
        if not market:
            return None
        # Market may have tokens directly or nested
        tokens = {}
        if isinstance(market, dict):
            # Try different structures
            for key in ("tokens", "clobTokenIds", "clob_token_ids"):
                if key in market and market[key]:
                    tok_data = market[key]
                    if isinstance(tok_data, list):
                        for t in tok_data:
                            if isinstance(t, dict):
                                outcome = t.get("outcome", "").upper()
                                token_id = str(t.get("token_id", t.get("tokenId", "")))
                                if outcome and token_id:
                                    tokens[outcome] = token_id
                    elif isinstance(tok_data, dict):
                        tokens = {k.upper(): str(v) for k, v in tok_data.items()}
            # Also check outcomePrices / outcomes
            if not tokens:
                outcomes = market.get("outcomes", [])
                clob_ids = market.get("clobTokenIds", [])
                if isinstance(outcomes, list) and isinstance(clob_ids, list):
                    for o, tid in zip(outcomes, clob_ids):
                        tokens[str(o).upper()] = str(tid)
        return tokens if tokens else None
    except Exception as e:
        log(f"Failed to get market tokens for {slug}: {e}")
        return None


def get_current_exposure() -> float:
    """Get current total position value."""
    try:
        result = run_cli(["clob", "balance"])
        if isinstance(result, dict):
            # Check various balance fields
            for key in ("balance", "total_value", "totalValue"):
                if key in result:
                    return float(result[key])
        return 0.0
    except Exception as e:
        log(f"Failed to get balance: {e}")
        return 0.0


def place_order(token_id: str, side: str, price: float, size: float) -> Dict[str, Any]:
    """Place a limit order. Returns result dict."""
    if DRY_RUN:
        log(f"DRY RUN: would place {side} {size} shares @ ${price:.3f} (token: {token_id})")
        return {"dry_run": True, "success": True}

    try:
        result = run_cli([
            "clob", "create-order",
            "--token", token_id,
            "--side", side,
            "--price", f"{price:.3f}",
            "--size", f"{size:.1f}",
            "--order-type", "GTC",
        ])
        return {"success": True, "result": result}
    except Exception as e:
        log(f"Order failed: {e}")
        return {"success": False, "error": str(e)}


def find_actionable_trades(suspects_data: Dict) -> List[Dict]:
    """Extract actionable trades from suspects.json."""
    trades = []
    rows = suspects_data.get("suspects", [])

    for row in rows:
        if row.get("tier") != "core":
            continue

        wallet = row.get("wallet", "")
        username = row.get("username", "")
        win_rate = row.get("win_rate", 0)
        score = row.get("score", 0)

        for action in (row.get("recent_actions") or []):
            side = (action.get("side") or "").upper()
            size = float(action.get("size", 0))
            price = float(action.get("price", 0))
            slug = action.get("slug", "")
            outcome = (action.get("outcome") or "").upper()
            title = action.get("title") or slug

            # Filter
            if ONLY_BUY and side != "BUY":
                continue
            if size < MIN_WHALE_SIZE:
                continue
            if price < MIN_PRICE or price > MAX_PRICE:
                continue
            if not slug or not outcome:
                continue

            trades.append({
                "wallet": wallet,
                "username": username,
                "score": score,
                "win_rate": win_rate,
                "side": side,
                "size": size,
                "price": price,
                "slug": slug,
                "outcome": outcome,
                "title": title,
            })

    return trades


def main() -> int:
    log(f"Starting copy trader (dry_run={DRY_RUN}, max_per_trade=${MAX_PER_TRADE}, max_exposure=${MAX_TOTAL_EXPOSURE})")

    suspects_data = load_json(SUSPECTS_FILE)
    if not suspects_data:
        log("No suspects.json found or empty")
        return 0

    copy_state = load_json(COPY_STATE_FILE)
    copied_markets = set(copy_state.get("copied_markets", []))
    total_invested = float(copy_state.get("total_invested", 0))
    trade_log = copy_state.get("trades", [])

    actionable = find_actionable_trades(suspects_data)
    if not actionable:
        log("No actionable trades found")
        return 0

    log(f"Found {len(actionable)} actionable trades")
    executed = []

    for trade in actionable:
        slug = trade["slug"]
        outcome = trade["outcome"]
        market_key = f"{slug}:{outcome}"

        # Skip if already copied this market+outcome
        if market_key in copied_markets:
            log(f"Already copied {market_key}, skipping")
            continue

        # Check exposure limit
        if total_invested + MAX_PER_TRADE > MAX_TOTAL_EXPOSURE:
            log(f"Exposure limit reached (${total_invested:.2f} / ${MAX_TOTAL_EXPOSURE})")
            break

        # Get token ID
        tokens = get_market_tokens(slug)
        if not tokens:
            log(f"Could not get tokens for {slug}")
            continue

        token_id = tokens.get(outcome) or tokens.get("YES")
        if not token_id:
            log(f"No token found for outcome {outcome} in {slug}")
            continue

        # Calculate shares: $5 / price = shares
        shares = MAX_PER_TRADE / trade["price"]

        log(f"Placing order: {slug} {outcome} {shares:.1f} shares @ ${trade['price']:.3f}")
        result = place_order(token_id, "buy", trade["price"], shares)

        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "slug": slug,
            "title": trade["title"],
            "outcome": outcome,
            "token_id": token_id,
            "price": trade["price"],
            "shares": round(shares, 1),
            "amount_usd": MAX_PER_TRADE,
            "whale_wallet": trade["wallet"],
            "whale_username": trade["username"],
            "whale_score": trade["score"],
            "whale_size": trade["size"],
            "result": result,
        }

        if result.get("success"):
            copied_markets.add(market_key)
            total_invested += MAX_PER_TRADE
            executed.append(record)
            trade_log.append(record)
            log(f"✅ Order placed: {slug} {outcome}")
        else:
            log(f"❌ Order failed: {slug} {outcome}")
            trade_log.append(record)

    # Save state
    copy_state = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "copied_markets": list(copied_markets),
        "total_invested": round(total_invested, 2),
        "trades": trade_log[-100:],  # keep last 100
    }
    save_json(COPY_STATE_FILE, copy_state)

    # Telegram notification
    if executed:
        ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")
        lines = [f"🤖 <b>自动跟单执行</b> ({ts})", ""]
        for i, t in enumerate(executed, 1):
            dry = " [模拟]" if DRY_RUN else ""
            lines.append(
                f"{i}) <b>{t['title']}</b>\n"
                f"   {t['outcome']} ${t['amount_usd']:.0f} ({t['shares']:.1f}股) @ {t['price']:.3f}{dry}\n"
                f"   跟随: {t['whale_username']} (分数{t['whale_score']}, 鲸鱼下注${t['whale_size']:,.0f})"
            )
        lines.append(f"\n💰 累计投入: ${total_invested:.2f} / ${MAX_TOTAL_EXPOSURE}")
        tg_send("\n".join(lines))

    log(f"Done. Executed {len(executed)} trades, total invested: ${total_invested:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
