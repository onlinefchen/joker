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
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
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
MIN_TRADE_SIZE = float(os.environ.get("COPY_MIN_TRADE_SIZE", "1.00"))    # don't trade below $1.00
MIN_SHARES = 5  # Polymarket minimum order size is 5 shares
MIN_WHALE_SIZE = float(os.environ.get("COPY_MIN_WHALE_SIZE", "100000"))  # only copy ≥$100K trades
MIN_PRICE = float(os.environ.get("COPY_MIN_PRICE", "0.05"))             # don't buy below 5¢
MAX_PRICE = float(os.environ.get("COPY_MAX_PRICE", "0.92"))             # don't buy above 92¢
ONLY_BUY = os.environ.get("COPY_ONLY_BUY", "true").lower() == "true"  # only follow BUY
DRY_RUN = os.environ.get("COPY_DRY_RUN", "false").lower() == "true"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

PERFORMANCE_HISTORY_CAP = 500


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
        err = p.stderr.strip() or p.stdout.strip() or "(no output)"
        raise RuntimeError(f"CLI failed ({p.returncode}): {err}")
    out = p.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        log(f"CLI raw output: {out[:200]}")
        return None


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
        tokens = {}
        if isinstance(market, dict):
            # Parse outcomes and clobTokenIds (may be JSON strings)
            outcomes_raw = market.get("outcomes", [])
            if isinstance(outcomes_raw, str):
                try:
                    outcomes_raw = json.loads(outcomes_raw)
                except Exception:
                    outcomes_raw = []

            clob_ids_raw = market.get("clobTokenIds", market.get("clob_token_ids", []))
            if isinstance(clob_ids_raw, str):
                try:
                    clob_ids_raw = json.loads(clob_ids_raw)
                except Exception:
                    clob_ids_raw = []

            # Map outcomes to token IDs by position
            if outcomes_raw and clob_ids_raw and len(outcomes_raw) == len(clob_ids_raw):
                for outcome, token_id in zip(outcomes_raw, clob_ids_raw):
                    tokens[outcome.upper()] = token_id
                if tokens:
                    log(f"Tokens for {slug}: {list(tokens.keys())}")
                    return tokens

            # Fallback: try different structures
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
        result = run_cli(["clob", "balance", "--asset-type", "collateral"], use_proxy=True)
        if isinstance(result, dict):
            bal = float(result.get("balance", 0))
            log(f"Available balance: ${bal:.2f}")
            return bal
        return 0.0
    except Exception as e:
        log(f"Failed to get balance: {e}")
        return 0.0


def calc_trade_size(available_balance: float) -> float:
    """Calculate trade size: 20% of available balance, but at least MIN_TRADE_SIZE.

    If 20% of balance < MIN_TRADE_SIZE but balance >= MIN_TRADE_SIZE,
    use MIN_TRADE_SIZE instead of skipping the trade entirely.
    """
    if _MAX_PER_TRADE_ENV > 0:
        return _MAX_PER_TRADE_ENV
    size = available_balance * BALANCE_PERCENT
    if size < MIN_TRADE_SIZE and available_balance >= MIN_TRADE_SIZE:
        size = MIN_TRADE_SIZE
    return round(size, 2)


def get_token_balance(token_id: str) -> float:
    """Get our balance (shares held) for a conditional token."""
    try:
        result = run_cli([
            "clob", "balance",
            "--asset-type", "conditional",
            "--token", token_id,
        ], use_proxy=True)
        if isinstance(result, dict):
            return float(result.get("balance", 0))
    except Exception as e:
        log(f"Failed to get token balance: {e}")
    return 0.0


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

            # Filter: skip all SELL actions (simplified)
            if side != "BUY":
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


# ─── Win Rate Tracking ────────────────────────────────────────────

def _get_market_winner(slug: str) -> Optional[str]:
    """Query a market and return the winning outcome (uppercase), or None if not resolved."""
    try:
        market = run_cli(["markets", "get", slug])
        if not isinstance(market, dict):
            return None

        # Check tokens array for winner field
        tokens = market.get("tokens", [])
        if isinstance(tokens, list):
            for t in tokens:
                if isinstance(t, dict) and t.get("winner") is True:
                    return str(t.get("outcome", "")).upper()

        # Fallback: check outcomes + outcomePrices (resolved market has 1.0 for winner)
        outcomes_raw = market.get("outcomes", [])
        if isinstance(outcomes_raw, str):
            try:
                outcomes_raw = json.loads(outcomes_raw)
            except Exception:
                outcomes_raw = []
        prices_raw = market.get("outcomePrices", [])
        if isinstance(prices_raw, str):
            try:
                prices_raw = json.loads(prices_raw)
            except Exception:
                prices_raw = []
        if outcomes_raw and prices_raw and len(outcomes_raw) == len(prices_raw):
            for outcome, price in zip(outcomes_raw, prices_raw):
                try:
                    if float(price) >= 0.99:
                        return str(outcome).upper()
                except (ValueError, TypeError):
                    pass

        return None
    except Exception as e:
        log(f"Failed to get market winner for {slug}: {e}")
        return None


def _find_trade_for_position(trade_log: List[Dict], slug: str, outcome: str) -> Optional[Dict]:
    """Find the most recent successful BUY trade matching this position."""
    for t in reversed(trade_log):
        if (t.get("slug") == slug
                and (t.get("outcome") or "").upper() == outcome.upper()
                and t.get("result", {}).get("success")):
            return t
    return None


def _init_performance(copy_state: Dict) -> Dict:
    """Ensure performance dict exists with correct structure."""
    perf = copy_state.get("performance", {})
    perf.setdefault("total_settled", 0)
    perf.setdefault("wins", 0)
    perf.setdefault("losses", 0)
    perf.setdefault("total_pnl_usd", 0.0)
    perf.setdefault("win_rate_pct", 0.0)
    perf.setdefault("history", [])
    return perf


def _update_performance_counters(perf: Dict):
    """Recalculate win_rate_pct from wins/total_settled."""
    total = perf["total_settled"]
    perf["win_rate_pct"] = round((perf["wins"] / total) * 100, 1) if total > 0 else 0.0
    perf["total_pnl_usd"] = round(perf["total_pnl_usd"], 2)


def check_settlement_results(
    copied_positions: Dict[str, Dict],
    trade_log: List[Dict],
    performance: Dict,
) -> List[Dict]:
    """Check newly settled positions for win/loss and record performance.

    Returns a list of settlement records for Telegram notification.
    """
    settlements = []

    for key, pos in copied_positions.items():
        if not pos.get("settled"):
            continue
        # Skip if already recorded in performance history
        if pos.get("perf_recorded"):
            continue

        pos_slug = pos.get("slug") or key.split(":")[0]
        pos_outcome = (pos.get("outcome") or key.split(":")[1] if ":" in key else "").upper()
        pos_side = pos.get("side", "BUY")
        whale_username = pos.get("username", "unknown")

        # Only track BUY positions for win/loss (SELLs are exits)
        if pos_side != "BUY":
            pos["perf_recorded"] = True
            continue

        # Determine winner
        winner = _get_market_winner(pos_slug)
        if winner is None:
            # Market data unavailable or not yet resolved — skip for now
            # (settled flag may have been set prematurely; we'll retry next run)
            log(f"Winner unknown for {pos_slug}, will retry")
            continue

        # Find our trade details
        trade = _find_trade_for_position(trade_log, pos_slug, pos_outcome)
        buy_price = float(trade.get("price", 0)) if trade else 0.0
        shares = float(trade.get("shares", 0)) if trade else 0.0

        won = pos_outcome.upper() == winner.upper()
        if won:
            pnl = (1.0 - buy_price) * shares
        else:
            pnl = -buy_price * shares
        pnl = round(pnl, 2)

        # Record
        record = {
            "slug": pos_slug,
            "outcome": pos_outcome,
            "side": pos_side,
            "buy_price": buy_price,
            "shares": shares,
            "won": won,
            "pnl_usd": pnl,
            "winning_outcome": winner,
            "settled_at": datetime.now(timezone.utc).isoformat(),
            "whale_username": whale_username,
        }

        performance["history"].append(record)
        performance["total_settled"] += 1
        if won:
            performance["wins"] += 1
        else:
            performance["losses"] += 1
        performance["total_pnl_usd"] += pnl

        _update_performance_counters(performance)

        # Cap history
        if len(performance["history"]) > PERFORMANCE_HISTORY_CAP:
            performance["history"] = performance["history"][-PERFORMANCE_HISTORY_CAP:]

        pos["perf_recorded"] = True
        settlements.append(record)

        emoji = "🏆" if won else "💀"
        log(f"{emoji} Settlement: {pos_slug} {pos_outcome} → winner={winner}, pnl=${pnl:+.2f}")

    return settlements


def _format_performance_summary(performance: Dict) -> str:
    """Format a short performance summary string."""
    total = performance.get("total_settled", 0)
    wins = performance.get("wins", 0)
    losses = performance.get("losses", 0)
    pnl = performance.get("total_pnl_usd", 0.0)
    wr = performance.get("win_rate_pct", 0.0)
    if total == 0:
        return "📊 胜率: 暂无数据"
    return f"📊 胜率: {wins}/{total} ({wr:.1f}%) | P&L: ${pnl:+.2f}"


def main() -> int:
    log(f"Starting copy trader (dry_run={DRY_RUN}, balance_pct={BALANCE_PERCENT:.0%})")

    # Auto-redeem resolved markets first
    redeemed = auto_redeem()
    if redeemed:
        log(f"Auto-redeemed {len(redeemed)} markets")

    suspects_data = load_json(SUSPECTS_FILE)
    if not suspects_data:
        log("No suspects.json found or empty")
        return 0

    copy_state = load_json(COPY_STATE_FILE)
    # New format: copied_positions tracks {market_key: {wallet, settled, trade_time}}
    copied_positions = copy_state.get("copied_positions", {})
    # Migrate old format
    for old_key in copy_state.get("copied_markets", []):
        if old_key not in copied_positions:
            copied_positions[old_key] = {"wallet": "unknown", "settled": False}
    trade_log = copy_state.get("trades", [])
    performance = _init_performance(copy_state)

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
        wallet = trade["wallet"]
        whale_side = trade["side"]
        market_key = f"{slug}:{outcome}:{whale_side}"

        # Only process BUY trades (SELL handling removed)
        if whale_side != "BUY":
            continue
            
        # Skip if already copied this BUY (same market+outcome)
        if market_key in copied_positions and not copied_positions[market_key].get("settled"):
            log(f"Already copied {market_key} (unsettled), skipping")
            continue

        # Check balance and exposure limits for BUY
        trade_size = calc_trade_size(available)
        if trade_size < MIN_TRADE_SIZE:
            log(f"Trade size too small (${trade_size:.2f}), stopping")
            break

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

        # Only handle BUY trades (whale sells outcome → we buy same outcome)
        token_id = tokens.get(outcome) or tokens.get(outcome.upper()) or tokens.get("YES")
        if not token_id:
            # Try case-insensitive match
            for k, v in tokens.items():
                if k.upper() == outcome.upper():
                    token_id = v
                    break
        if not token_id:
            log(f"No token found for outcome {outcome} in {slug}, available: {list(tokens.keys())}")
            continue

        # Calculate shares (minimum 5)
        shares = trade_size / trade["price"]
        if shares < MIN_SHARES:
            shares = MIN_SHARES
            trade_size = shares * trade["price"]

        log(f"Following BUY: {slug} {outcome} ${trade_size:.2f} (market order)")
        result = place_market_order(token_id, "buy", trade_size)

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
            # Record the BUY position
            copied_positions[market_key] = {
                "wallet": wallet,
                "username": trade["username"],
                "settled": False,
                "trade_time": datetime.now(timezone.utc).isoformat(),
                "slug": slug,
                "outcome": outcome,
                "side": whale_side,
            }
            available -= trade_size
            executed.append(record)
            trade_log.append(record)
            log(f"✅ Order placed: BUY {slug} {outcome}")
        else:
            log(f"❌ Order failed: BUY {slug} {outcome}")
            trade_log.append(record)

    # Mark settled positions (check market resolution status)
    for key, pos in copied_positions.items():
        if pos.get("settled"):
            continue
        pos_slug = pos.get("slug") or key.split(":")[0]
        try:
            market = run_cli(["markets", "get", pos_slug])
            if isinstance(market, dict) and (market.get("resolved") or market.get("closed")):
                pos["settled"] = True
                log(f"Marked settled: {pos_slug}")
        except Exception:
            pass

    # Check settlement results and record win/loss performance
    settlements = check_settlement_results(copied_positions, trade_log, performance)

    # Send Telegram notification for each settlement
    if settlements:
        ts = datetime.now(CST).strftime("%m-%d %H:%M CST")
        lines = [f"🏁 <b>仓位结算通知</b> ({ts})", ""]
        for s in settlements:
            emoji = "🏆" if s["won"] else "💀"
            lines.append(
                f"{emoji} <b>{s['slug']}</b>\n"
                f"   持仓: {s['outcome']} @ ${s['buy_price']:.3f} × {s['shares']:.1f}股\n"
                f"   胜出: {s['winning_outcome']} | P&L: <b>${s['pnl_usd']:+.2f}</b>\n"
                f"   跟随: {s['whale_username']}"
            )
        lines.append("")
        lines.append(_format_performance_summary(performance))
        tg_send("\n".join(lines))

    # Save state
    copy_state = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "copied_positions": copied_positions,
        "trades": trade_log[-200:],
        "performance": performance,
    }
    save_json(COPY_STATE_FILE, copy_state)

    # Telegram notification for executed trades
    if executed:
        ts = datetime.now(CST).strftime("%m-%d %H:%M CST")
        lines = [f"🤖 <b>自动跟单执行</b> ({ts})", ""]
        for i, t in enumerate(executed, 1):
            side_emoji = "📗"  # Always BUY now
            lines.append(
                f"{i}) {side_emoji} <b>{t['title']}</b>\n"
                f"   {t['outcome']} ${t['amount_usd']:.2f} ({t['shares']:.1f}股) @ {t['price']:.3f}\n"
                f"   跟随: {t['whale_username']} (分数{t['whale_score']}, 鲸鱼下注${t['whale_size']:,.0f})"
            )
        lines.append(f"\n💰 余额: ${available:.2f}")
        # Append win rate stats
        if performance["total_settled"] > 0:
            lines.append(_format_performance_summary(performance))
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
    performance = _init_performance(copy_state)

    # 4. Build message
    ts = datetime.now(CST).strftime("%m-%d %H:%M CST")
    lines = [
        f"📊 <b>Polymarket 每日报告</b> ({ts})",
        f"━━━━━━━━━━━━━━━",
        f"💰 可用余额: <b>${available:.2f}</b>",
        f"📈 累计跟单: ${total_invested:.2f} ({len(successful)} 笔)",
        "",
    ]

    # ── Performance / Win Rate Section ──
    if performance["total_settled"] > 0:
        lines.append("🎯 <b>跟单胜率:</b>")
        lines.append(
            f"  胜率: {performance['wins']}/{performance['total_settled']} "
            f"({performance['win_rate_pct']:.1f}%)"
        )
        lines.append(f"  总盈亏: <b>${performance['total_pnl_usd']:+.2f}</b>")

        history = performance.get("history", [])
        if history:
            best = max(history, key=lambda h: h.get("pnl_usd", 0))
            worst = min(history, key=lambda h: h.get("pnl_usd", 0))
            lines.append(
                f"  最佳: {best['slug']} {best.get('outcome', '')} "
                f"${best['pnl_usd']:+.2f}"
            )
            lines.append(
                f"  最差: {worst['slug']} {worst.get('outcome', '')} "
                f"${worst['pnl_usd']:+.2f}"
            )
        lines.append("")
    else:
        lines.append("🎯 跟单胜率: 暂无结算数据\n")

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
            lines.append(f"  ✅ {r.get('title') or r.get('question', '?')}")
        lines.append("")

    # Copied positions summary
    positions = copy_state.get("copied_positions", {})
    open_count = sum(1 for p in positions.values() if not p.get("settled"))
    settled_count = sum(1 for p in positions.values() if p.get("settled"))
    # Protected wallets (have unsettled positions)
    protected_wallets = set(p.get("wallet") for p in positions.values() if not p.get("settled") and p.get("wallet"))
    lines.append(f"🔒 跟单仓位: {open_count} 个未结算, {settled_count} 个已结算")
    if protected_wallets:
        lines.append(f"🛡️ 保护地址: {len(protected_wallets)} 个（有未结算仓位，不会被降级）")

    tg_send("\n".join(lines))
    log("Daily report sent")
    return 0


# Wallet address for position queries / daily report.
# Intentionally no default to avoid leaking a real operator wallet.
WALLET_ADDRESS = os.environ.get("POLYMARKET_WALLET", "").strip()


def auto_redeem() -> List[Dict]:
    """Check ALL traded markets (not just copy trades) and redeem resolved ones.

    Uses the authenticated CLOB trades API to discover every market the wallet
    has ever traded, then checks each for resolution and attempts redemption.
    This covers both copy-traded and manually-placed positions.
    """
    redeemed = []

    # --- 1. Discover all markets from CLOB trade history ---
    try:
        trades_data = run_cli(["clob", "trades"], use_proxy=True)
    except RuntimeError as e:
        log(f"Failed to fetch CLOB trades: {e}")
        # Fallback: try copy_state.json for backwards compat
        return _auto_redeem_from_copy_state()

    if not trades_data:
        log("No CLOB trades found")
        return redeemed

    # CLOB trades response may be {data: [...]} or [...]
    trade_list = trades_data.get("data", trades_data) if isinstance(trades_data, dict) else trades_data
    if not isinstance(trade_list, list):
        log(f"Unexpected CLOB trades format: {type(trade_list)}")
        return redeemed

    # Unique condition IDs
    condition_ids = {}
    for t in trade_list:
        cid = t.get("market", "")
        if cid and cid not in condition_ids:
            condition_ids[cid] = t.get("outcome", "")

    log(f"Found {len(condition_ids)} unique markets in trade history")

    # Track already-redeemed to avoid redundant on-chain calls
    redeemed_file = Path("redeemed_markets.json")
    already_redeemed: set = set()
    if redeemed_file.exists():
        try:
            already_redeemed = set(json.loads(redeemed_file.read_text()).get("markets", []))
        except Exception:
            pass

    # --- 2. Check each market and redeem if resolved ---
    for cid in condition_ids:
        if cid in already_redeemed:
            continue

        try:
            market = run_cli(["clob", "market", cid])
            if not isinstance(market, dict):
                continue

            closed = market.get("closed", False)
            active = market.get("active", True)

            if not closed:
                continue

            neg_risk = market.get("neg_risk", False)
            question = market.get("question", cid[:20])

            # Skip neg-risk markets: redeem-neg-risk requires exact token
            # amounts which we can't reliably determine from trade history.
            # These are best redeemed manually or via a dedicated script.
            if neg_risk:
                log(f"Skipping neg-risk market (manual redeem needed): {question[:50]}")
                continue

            log(f"Redeeming: {question[:50]} (condition: {cid[:16]}...)")

            try:
                result = run_cli([
                    "ctf", "redeem",
                    "--condition", cid,
                ], use_proxy=True, timeout=60)

                tx_hash = ""
                if isinstance(result, dict):
                    tx_hash = result.get("transaction_hash", "")

                redeemed.append({
                    "question": question,
                    "condition_id": cid,
                    "tx": tx_hash,
                })
                already_redeemed.add(cid)
                log(f"✅ Redeemed: {question[:50]}")

            except RuntimeError as e:
                err = str(e).lower()
                if any(skip in err for skip in ["nothing", "no payout", "already", "zero", "revert", "payout is zero"]):
                    log(f"Nothing to redeem: {question[:50]}")
                    already_redeemed.add(cid)  # Don't retry
                else:
                    log(f"Redeem failed: {question[:50]} - {e}")

        except Exception as e:
            log(f"Error checking market {cid[:16]}...: {e}")

    # Persist redeemed set (keep last 500)
    try:
        redeemed_list = list(already_redeemed)[-500:]
        redeemed_file.write_text(json.dumps({"markets": redeemed_list}))
    except Exception:
        pass

    return redeemed


def _auto_redeem_from_copy_state() -> List[Dict]:
    """Fallback: redeem only from copy_state.json (old behavior)."""
    redeemed = []
    copy_state = load_json(COPY_STATE_FILE)
    trades = copy_state.get("trades", [])
    successful = [t for t in trades if t.get("result", {}).get("success")]

    if not successful:
        return redeemed

    seen_slugs: set = set()
    for trade in successful:
        slug = trade.get("slug", "")
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        try:
            market = run_cli(["markets", "get", slug])
            if not isinstance(market, dict):
                continue

            closed = market.get("closed", False)
            active = market.get("active", True)
            if not (closed and not active):
                continue

            condition_id = market.get("conditionId") or market.get("condition_id", "")
            if not condition_id:
                continue

            neg_risk = market.get("negRisk", market.get("neg_risk", False))
            if neg_risk:
                continue

            result = run_cli(["ctf", "redeem", "--condition", condition_id], use_proxy=True, timeout=60)
            redeemed.append({
                "question": market.get("question", slug),
                "condition_id": condition_id,
                "tx": result.get("transaction_hash", "") if isinstance(result, dict) else "",
            })
            log(f"✅ Redeemed (fallback): {slug}")

        except Exception as e:
            log(f"Error in fallback redeem for {slug}: {e}")

    return redeemed


if __name__ == "__main__":
    import sys as _sys
    if "--daily-report" in _sys.argv:
        raise SystemExit(daily_report())
    raise SystemExit(main())
