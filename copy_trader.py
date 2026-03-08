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
# MAX_PER_TRADE=0 means dynamic: 20% of available balance per trade
_MAX_PER_TRADE_ENV = float(os.environ.get("COPY_MAX_PER_TRADE", "0"))
# MAX_EXPOSURE=0 means no hard cap (rely on per-trade % limit)
_MAX_EXPOSURE_ENV = float(os.environ.get("COPY_MAX_EXPOSURE", "0"))
BALANCE_PERCENT = float(os.environ.get("COPY_BALANCE_PERCENT", "0.20"))  # 20% of available balance
MIN_TRADE_SIZE = float(os.environ.get("COPY_MIN_TRADE_SIZE", "0.50"))    # don't trade below $0.50
MIN_SHARES = 5  # Polymarket minimum order size is 5 shares
MIN_WHALE_SIZE = float(os.environ.get("COPY_MIN_WHALE_SIZE", "100000"))  # only copy ≥$100K trades
MIN_PRICE = float(os.environ.get("COPY_MIN_PRICE", "0.05"))             # don't buy below 5¢
MAX_PRICE = float(os.environ.get("COPY_MAX_PRICE", "0.92"))             # don't buy above 92¢
ONLY_BUY = os.environ.get("COPY_ONLY_BUY", "false").lower() == "true"  # follow both BUY and SELL
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


USE_PROXYCHAINS = os.environ.get("USE_PROXYCHAINS", "false").lower() == "true"


def run_cli(args: List[str], timeout: int = 30, use_proxy: bool = False) -> Any:
    base_cmd = ["polymarket", *args, "-o", "json"]
    # Wrap with proxychains for trading operations (geoblock bypass)
    if use_proxy and USE_PROXYCHAINS:
        cmd = ["proxychains4", "-q"] + base_cmd
    else:
        cmd = base_cmd
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


def get_available_balance() -> float:
    """Get available USDC balance (collateral)."""
    try:
        result = run_cli(["clob", "balance", "--asset-type", "collateral"])
        if isinstance(result, dict):
            bal = float(result.get("balance", 0))
            log(f"Available balance: ${bal:.2f}")
            return bal
        return 0.0
    except Exception as e:
        log(f"Failed to get balance: {e}")
        return 0.0


def calc_trade_size(available_balance: float) -> float:
    """Calculate trade size: 20% of available balance or fixed amount."""
    if _MAX_PER_TRADE_ENV > 0:
        return _MAX_PER_TRADE_ENV
    size = available_balance * BALANCE_PERCENT
    return round(size, 2)


def place_market_order(token_id: str, side: str, amount: float) -> Dict[str, Any]:
    """Place a market order via proxy. amount = USDC for buys, shares for sells."""
    if DRY_RUN:
        log(f"DRY RUN: would market {side} ${amount:.2f} (token: {token_id})")
        return {"dry_run": True, "success": True}

    try:
        result = run_cli([
            "clob", "market-order",
            "--token", token_id,
            "--side", side,
            "--amount", f"{amount:.2f}",
        ], use_proxy=True)
        return {"success": True, "result": result}
    except Exception as e:
        log(f"Market order failed: {e}")
        return {"success": False, "error": str(e)}


def place_order(token_id: str, side: str, price: float, size: float) -> Dict[str, Any]:
    """Place a limit order via proxy. Returns result dict."""
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
        ], use_proxy=True)
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
    log(f"Starting copy trader (dry_run={DRY_RUN}, balance_pct={BALANCE_PERCENT:.0%})")

    # Auto-redeem resolved markets first
    redeemed = auto_redeem()
    if redeemed:
        ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")
        lines = [f"💸 <b>自动领取收益</b> ({ts})", ""]
        for r in redeemed:
            lines.append(f"  ✅ {r['title']}")
        tg_send("\n".join(lines))

    suspects_data = load_json(SUSPECTS_FILE)
    if not suspects_data:
        log("No suspects.json found or empty")
        return 0

    copy_state = load_json(COPY_STATE_FILE)
    copied_markets = set(copy_state.get("copied_markets", []))
    trade_log = copy_state.get("trades", [])

    actionable = find_actionable_trades(suspects_data)
    if not actionable:
        log("No actionable trades found")
        return 0

    log(f"Found {len(actionable)} actionable trades")

    # Get live balance
    available = get_available_balance()
    if available < MIN_TRADE_SIZE:
        log(f"Balance too low (${available:.2f}), skipping")
        return 0

    executed = []

    for trade in actionable:
        slug = trade["slug"]
        outcome = trade["outcome"]
        market_key = f"{slug}:{outcome}"

        # Skip if already copied this market+outcome
        if market_key in copied_markets:
            log(f"Already copied {market_key}, skipping")
            continue

        # Recalculate trade size from current balance (refreshed each trade)
        trade_size = calc_trade_size(available)
        if trade_size < MIN_TRADE_SIZE:
            log(f"Trade size too small (${trade_size:.2f}), stopping")
            break

        # Check hard exposure cap if set
        if _MAX_EXPOSURE_ENV > 0:
            total_invested = sum(t.get("amount_usd", 0) for t in trade_log if t.get("result", {}).get("success"))
            if total_invested + trade_size > _MAX_EXPOSURE_ENV:
                log(f"Exposure limit reached (${total_invested:.2f} / ${_MAX_EXPOSURE_ENV})")
                break

        # Get token ID
        tokens = get_market_tokens(slug)
        if not tokens:
            log(f"Could not get tokens for {slug}")
            continue

        whale_side = trade["side"]  # BUY or SELL

        if whale_side == "BUY":
            # Whale buys outcome → we buy same outcome
            token_id = tokens.get(outcome) or tokens.get("YES")
            if not token_id:
                log(f"No token found for outcome {outcome} in {slug}")
                continue

            # Calculate shares (minimum 5)
            shares = trade_size / trade["price"]
            if shares < MIN_SHARES:
                shares = MIN_SHARES
                trade_size = shares * trade["price"]

            log(f"Following BUY: {slug} {outcome} {shares:.1f} shares @ ${trade['price']:.3f} (${trade_size:.2f})")
            result = place_order(token_id, "buy", trade["price"], shares)

        else:
            # Whale sells outcome → we buy the opposite side
            # e.g., whale sells YES → we buy NO (betting against YES)
            opposite = "NO" if outcome == "YES" else "YES"
            token_id = tokens.get(opposite)
            if not token_id:
                log(f"No token found for opposite {opposite} in {slug}")
                continue

            opposite_price = 1.0 - trade["price"]  # complement price
            if opposite_price < MIN_PRICE or opposite_price > MAX_PRICE:
                log(f"Opposite price ${opposite_price:.3f} out of range")
                continue

            shares = trade_size / opposite_price
            if shares < MIN_SHARES:
                shares = MIN_SHARES
                trade_size = shares * opposite_price

            log(f"Following SELL: {slug} buying {opposite} {shares:.1f} shares @ ${opposite_price:.3f} (${trade_size:.2f})")
            result = place_order(token_id, "buy", opposite_price, shares)

        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "slug": slug,
            "title": trade["title"],
            "outcome": outcome,
            "token_id": token_id,
            "price": trade["price"],
            "shares": round(shares, 1),
            "amount_usd": trade_size,
            "whale_wallet": trade["wallet"],
            "whale_username": trade["username"],
            "whale_score": trade["score"],
            "whale_size": trade["size"],
            "result": result,
        }

        if result.get("success"):
            copied_markets.add(market_key)
            available -= trade_size  # deduct from available
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
        "trades": trade_log[-200:],  # keep last 200
    }
    save_json(COPY_STATE_FILE, copy_state)

    # Telegram notification
    if executed:
        ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")
        lines = [f"🤖 <b>自动跟单执行</b> ({ts})", ""]
        for i, t in enumerate(executed, 1):
            lines.append(
                f"{i}) <b>{t['title']}</b>\n"
                f"   {t['outcome']} ${t['amount_usd']:.2f} ({t['shares']:.1f}股) @ {t['price']:.3f}\n"
                f"   跟随: {t['whale_username']} (分数{t['whale_score']}, 鲸鱼下注${t['whale_size']:,.0f})"
            )
        lines.append(f"\n💰 余额: ${available:.2f}")
        tg_send("\n".join(lines))

    log(f"Done. Executed {len(executed)} trades, remaining balance: ${available:.2f}")
    return 0


def daily_report() -> int:
    """Generate daily summary: open positions, settled, P&L, balance."""
    log("Generating daily report...")

    # 0. Auto-redeem first
    redeemed = auto_redeem()

    # 1. Balance (after redeem)
    available = get_available_balance()

    # 2. Open positions
    try:
        positions = run_cli(["data", "positions", WALLET_ADDRESS]) if WALLET_ADDRESS else []
        if not isinstance(positions, list):
            positions = []
    except Exception as e:
        log(f"Failed to get positions: {e}")
        positions = []

    # 3. Copy trade history
    copy_state = load_json(COPY_STATE_FILE)
    trades = copy_state.get("trades", [])
    successful = [t for t in trades if t.get("result", {}).get("success")]
    total_invested = sum(t.get("amount_usd", 0) for t in successful)

    # 4. Build message
    ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")
    lines = [
        f"📊 <b>Polymarket 每日报告</b> ({ts})",
        f"━━━━━━━━━━━━━━━",
        f"💰 可用余额: <b>${available:.2f}</b>",
        f"📈 累计跟单: ${total_invested:.2f} ({len(successful)} 笔)",
        "",
    ]

    # Open positions
    if positions:
        lines.append("📂 <b>当前持仓:</b>")
        for pos in positions[:10]:
            title = pos.get("title") or pos.get("market", {}).get("question", "?")
            outcome = pos.get("outcome", "?")
            size = float(pos.get("size", 0))
            avg_price = float(pos.get("avgPrice", pos.get("avg_price", 0)))
            cur_price = float(pos.get("curPrice", pos.get("cur_price", 0)))
            pnl = (cur_price - avg_price) * size if avg_price > 0 else 0
            pnl_pct = ((cur_price / avg_price) - 1) * 100 if avg_price > 0 else 0
            emoji = "🟢" if pnl >= 0 else "🔴"

            lines.append(
                f"  {emoji} <b>{title}</b>\n"
                f"    {outcome} | {size:.1f}股 @ ${avg_price:.3f} → ${cur_price:.3f}\n"
                f"    盈亏: ${pnl:.2f} ({pnl_pct:+.1f}%)"
            )
        lines.append("")
    else:
        lines.append("📂 当前无持仓\n")

    # Recent copy trades (last 5)
    if successful:
        lines.append("🤖 <b>近期跟单:</b>")
        for t in successful[-5:]:
            time_str = t.get("time", "")[:10]
            lines.append(
                f"  • {t.get('title', t.get('slug', '?'))}\n"
                f"    {t.get('outcome')} ${t.get('amount_usd', 0):.2f} @ {t.get('price', 0):.3f} ({time_str})"
            )
        lines.append("")

    # Redeemed
    if redeemed:
        lines.append("💸 <b>本次自动领取:</b>")
        for r in redeemed:
            lines.append(f"  ✅ {r['title']}")
        lines.append("")

    # Copied markets count
    copied = len(copy_state.get("copied_markets", []))
    lines.append(f"🔒 已跟单市场: {copied} 个（不会重复）")

    tg_send("\n".join(lines))
    log("Daily report sent")
    return 0


# Wallet address for position queries
WALLET_ADDRESS = os.environ.get("POLYMARKET_WALLET", "0x76Ce440a449475bDA2aB33780F21F6eB8200C1d9")


def auto_redeem() -> List[Dict]:
    """Check for resolved markets and redeem winning tokens."""
    redeemed = []

    # Get copy trade state to find markets we've traded
    copy_state = load_json(COPY_STATE_FILE)
    trades = copy_state.get("trades", [])
    successful = [t for t in trades if t.get("result", {}).get("success")]

    if not successful:
        return redeemed

    # Check each traded market
    seen_slugs = set()
    for trade in successful:
        slug = trade.get("slug", "")
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        try:
            market = run_cli(["markets", "get", slug])
            if not isinstance(market, dict):
                continue

            # Check if market is resolved
            resolved = market.get("resolved", False)
            closed = market.get("closed", False)
            active = market.get("active", True)

            if not (resolved or (closed and not active)):
                continue

            # Get condition ID
            condition_id = market.get("conditionId") or market.get("condition_id", "")
            if not condition_id:
                continue

            # Check neg_risk flag
            neg_risk = market.get("negRisk", market.get("neg_risk", False))

            log(f"Found resolved market: {slug} (condition: {condition_id[:12]}..., neg_risk: {neg_risk})")

            # Try to redeem
            try:
                if neg_risk:
                    result = run_cli([
                        "ctf", "redeem-neg-risk",
                        "--condition", condition_id,
                        "--amounts", "1,1",
                    ], use_proxy=True)
                else:
                    result = run_cli([
                        "ctf", "redeem",
                        "--condition", condition_id,
                    ], use_proxy=True)

                redeemed.append({
                    "slug": slug,
                    "title": market.get("question", slug),
                    "condition_id": condition_id,
                    "result": result,
                })
                log(f"✅ Redeemed: {slug}")

            except RuntimeError as e:
                err = str(e)
                # "nothing to redeem" type errors are normal
                if any(skip in err.lower() for skip in ["nothing", "no payout", "already", "zero", "revert"]):
                    log(f"Nothing to redeem for {slug}: {err}")
                else:
                    log(f"Redeem failed for {slug}: {err}")

        except Exception as e:
            log(f"Error checking {slug}: {e}")
            continue

    return redeemed


if __name__ == "__main__":
    import sys as _sys
    if "--daily-report" in _sys.argv:
        raise SystemExit(daily_report())
    raise SystemExit(main())
