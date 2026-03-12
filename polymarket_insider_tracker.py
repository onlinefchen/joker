#!/usr/bin/env python3
"""
Polymarket insider-style wallet tracker (CLI-driven)

Usable mode features:
- Current score ranking
- Dynamic tiering (core / candidate / remove)
- Score momentum vs previous run
- Persistent state file for repeated scans

Example:
  python polymarket_insider_tracker.py --wallet-limit 80 --trades-limit 160 \
    --min-score 60 --state-file tracker_state.json --out suspects.json --report-md report.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class WalletReport:
    wallet: str
    username: str
    score: float
    reasons: List[str]
    metrics: Dict[str, Any]


def run_cli(args: List[str], timeout: int = 40) -> Any:
    cmd = ["polymarket", *args, "-o", "json"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"CLI failed: {' '.join(cmd)}\n{p.stderr.strip()}")
    out = p.stdout.strip()
    if not out:
        return None
    return json.loads(out)


def to_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def parse_ts(ts: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        return None


def parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return None


def filter_trades_by_lookback_hours(trades: List[Dict[str, Any]], lookback_hours: float) -> List[Dict[str, Any]]:
    if not lookback_hours or lookback_hours <= 0:
        return trades
    cutoff = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
    out: List[Dict[str, Any]] = []
    for t in trades:
        try:
            ts = int(t.get("timestamp", 0))
        except Exception:
            ts = 0
        if ts >= cutoff:
            out.append(t)
    return out


class MarketCache:
    def __init__(self):
        self.by_slug: Dict[str, Dict[str, Any]] = {}

    def get(self, slug: str) -> Optional[Dict[str, Any]]:
        if slug in self.by_slug:
            return self.by_slug[slug]
        try:
            m = run_cli(["markets", "get", slug])
            self.by_slug[slug] = m
            return m
        except Exception:
            self.by_slug[slug] = None
            return None


def summarize_recent_actions(
    trades: List[Dict[str, Any]],
    limit: int = 5,
    min_size: float = 100000,
    side_filter: str = "all",
    lookback_hours: float = 0,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    side_filter = (side_filter or "all").lower()
    trades = filter_trades_by_lookback_hours(trades, lookback_hours)

    for t in trades:
        side = (t.get("side") or "").upper()
        size = to_float(t.get("size"))
        if size < min_size:
            continue
        if side_filter in ("buy", "sell") and side != side_filter.upper():
            continue

        ts = parse_ts(t.get("timestamp"))
        items.append(
            {
                "time": ts.isoformat() if ts else str(t.get("timestamp", "")),
                "side": side,
                "size": round(size, 2),
                "price": round(to_float(t.get("price")), 4),
                "outcome": t.get("outcome") or "",
                "slug": t.get("slug") or "",
                "title": t.get("title") or "",
            }
        )
        if len(items) >= limit:
            break
    return items


def wallet_score(
    wallet: str,
    username: str,
    trades: List[Dict[str, Any]],
    unique_markets_traded: int,
    mcache: MarketCache,
    big_bet_usd: float,
    pre_end_hours_min: float,
    pre_end_hours_max: float,
) -> WalletReport:
    reasons: List[str] = []

    if not trades:
        return WalletReport(wallet, username, 0.0, ["no trades"], {"trade_count": 0})

    sizes = [to_float(t.get("size")) for t in trades if to_float(t.get("size")) > 0]
    median_size = statistics.median(sizes) if sizes else 0.0
    avg_size = statistics.mean(sizes) if sizes else 0.0
    max_size = max(sizes) if sizes else 0.0
    total_buy_size = sum(to_float(t.get("size")) for t in trades if (t.get("side") or "").upper() == "BUY")
    total_sell_size = sum(to_float(t.get("size")) for t in trades if (t.get("side") or "").upper() == "SELL")

    score_new_big = 0.0
    if unique_markets_traded <= 5:
        score_new_big += 12
        reasons.append(f"new-ish wallet (unique markets={unique_markets_traded})")
    large_bets = [s for s in sizes if s >= big_bet_usd]
    if large_bets:
        score_new_big += min(13, 5 + 2 * len(large_bets))
        reasons.append(f"{len(large_bets)} large bets >= ${big_bet_usd:,.0f}")

    slug_prefixes: Dict[str, int] = {}
    for t in trades:
        slug = t.get("slug") or ""
        pfx = slug.split("-")[0] if slug else "unknown"
        slug_prefixes[pfx] = slug_prefixes.get(pfx, 0) + 1
    top_bucket = max(slug_prefixes.values()) if slug_prefixes else 0
    focus_ratio = top_bucket / len(trades)

    score_focus = 0.0
    if focus_ratio >= 0.7 and len(trades) >= 8:
        score_focus = 18
        reasons.append(f"high vertical focus ({focus_ratio:.0%})")
    elif focus_ratio >= 0.55 and len(trades) >= 8:
        score_focus = 10
        reasons.append(f"moderate vertical focus ({focus_ratio:.0%})")

    score_abnormal = 0.0
    if median_size > 0:
        burst = [s for s in sizes if s >= max(big_bet_usd, 4 * median_size)]
        if burst:
            score_abnormal = min(20, 8 + 2 * len(burst))
            reasons.append(f"abnormal burst sizing ({len(burst)} trades >= max(threshold,4x median))")

    timed_hits = 0
    timed_total = 0
    for t in trades:
        if (t.get("side") or "").upper() != "BUY":
            continue
        slug = t.get("slug")
        tt = parse_ts(t.get("timestamp"))
        if not slug or not tt:
            continue
        m = mcache.get(slug)
        if not m:
            continue
        end_dt = parse_iso(m.get("endDate") or "")
        if not end_dt:
            continue
        delta_h = (end_dt - tt).total_seconds() / 3600
        timed_total += 1
        if pre_end_hours_min <= delta_h <= pre_end_hours_max:
            timed_hits += 1

    timing_ratio = (timed_hits / timed_total) if timed_total else 0.0
    score_timing = 0.0
    if timed_total >= 5 and timing_ratio >= 0.6:
        score_timing = 25
        reasons.append(f"precise pre-resolution timing ({timed_hits}/{timed_total})")
    elif timed_total >= 5 and timing_ratio >= 0.4:
        score_timing = 14
        reasons.append(f"some pre-resolution timing edge ({timed_hits}/{timed_total})")

    by_slug_ts: Dict[str, List[datetime]] = {}
    for t in trades:
        slug = t.get("slug")
        tt = parse_ts(t.get("timestamp"))
        if slug and tt:
            by_slug_ts.setdefault(slug, []).append(tt)

    fast_rounds = 0
    for ts_list in by_slug_ts.values():
        if len(ts_list) < 2:
            continue
        span_h = (max(ts_list) - min(ts_list)).total_seconds() / 3600
        if span_h <= 24:
            fast_rounds += 1

    score_fast = min(15, fast_rounds * 2)
    if fast_rounds >= 3:
        reasons.append(f"fast in/out across {fast_rounds} markets")

    score = score_new_big + score_focus + score_abnormal + score_timing + score_fast
    score = max(0.0, min(100.0, score))

    metrics = {
        "trade_count": len(trades),
        "unique_markets": unique_markets_traded,
        "median_size": round(median_size, 2),
        "avg_size": round(avg_size, 2),
        "max_size": round(max_size, 2),
        "total_buy_size": round(total_buy_size, 2),
        "total_sell_size": round(total_sell_size, 2),
        "focus_ratio": round(focus_ratio, 3),
        "timed_hits": timed_hits,
        "timed_total": timed_total,
        "timing_ratio": round(timing_ratio, 3),
        "fast_rounds": fast_rounds,
    }

    return WalletReport(wallet, username, round(score, 2), reasons, metrics)


def fetch_wallet_winrate(wallet: str, closed_limit: int = 100) -> Dict[str, Any]:
    try:
        closed = run_cli(["data", "closed-positions", wallet, "--limit", str(closed_limit)])
        if not isinstance(closed, list) or not closed:
            return {"win_rate": 0.0, "closed_count": 0, "wins": 0}
        wins = 0
        for p in closed:
            pnl = to_float(p.get("realized_pnl"))
            if pnl > 0:
                wins += 1
        n = len(closed)
        return {"win_rate": round(wins / n, 3), "closed_count": n, "wins": wins}
    except Exception:
        return {"win_rate": 0.0, "closed_count": 0, "wins": 0}


def load_state(path: str) -> Dict[str, Any]:
    # State loading only for output purposes - not used for scoring
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path: str, state: Dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_protected_wallets() -> set:
    """No longer protecting any wallets - simplified scoring"""
    return set()


# Global: no longer used for protection
_PROTECTED_WALLETS = set()


def classify_tier(score: float) -> str:
    """Simplified tier classification based only on current score"""
    if score >= 70:
        return "core"
    elif score >= 60:
        return "candidate"
    else:
        return "watch"


def build_report_md(title: str, generated_at: str, top_rows: List[Dict[str, Any]]) -> str:
    lines = [
        f"# {title}",
        "",
        f"Generated: {generated_at}",
        "",
        "| Rank | Wallet | User | Score | Tier |",
        "|---:|---|---|---:|---|",
    ]
    for i, r in enumerate(top_rows, 1):
        lines.append(
            f"| {i} | `{r['wallet']}` | {r['username']} | {r['score']:.1f} | {r['tier']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Polymarket insider-style tracker")
    ap.add_argument("--wallet-limit", type=int, default=80)
    ap.add_argument("--trades-limit", type=int, default=160)
    ap.add_argument("--lookback-hours", type=float, default=0, help="Only use trades in recent N hours; 0=disabled")
    ap.add_argument("--period", default="month", choices=["day", "week", "month", "all"])
    ap.add_argument("--order-by", default="pnl", choices=["pnl", "vol"])
    ap.add_argument("--big-bet", type=float, default=15000)
    ap.add_argument("--pre-end-hours-min", type=float, default=2.0)
    ap.add_argument("--pre-end-hours-max", type=float, default=24.0)
    ap.add_argument("--min-score", type=float, default=70.0)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--sleep-ms", type=int, default=120)
    ap.add_argument("--out", default="")
    ap.add_argument("--state-file", default="tracker_state.json")
    ap.add_argument("--report-md", default="")
    ap.add_argument("--action-min-size", type=float, default=100000, help="Only keep recent actions >= this size")
    ap.add_argument("--action-side", default="all", choices=["all", "buy", "sell"], help="Filter recent actions by side")
    ap.add_argument("--action-lookback-hours", type=float, default=0, help="Recent actions lookback window in hours; 0=disabled")
    ap.add_argument("--closed-limit", type=int, default=100, help="Closed positions sample size for win rate")
    args = ap.parse_args()

    board = run_cli([
        "data",
        "leaderboard",
        "--period",
        args.period,
        "--order-by",
        args.order_by,
        "--limit",
        str(args.wallet_limit),
    ])
    if not isinstance(board, list):
        print("Unexpected leaderboard response", file=sys.stderr)
        return 1

    # Still load state for debugging output, but don't use for scoring
    prev_state = load_state(args.state_file)

    mcache = MarketCache()
    reports: List[WalletReport] = []
    wallet_trades: Dict[str, List[Dict[str, Any]]] = {}

    for i, row in enumerate(board, 1):
        wallet = row.get("proxy_wallet")
        username = row.get("user_name") or wallet
        if not wallet:
            continue

        try:
            traded = run_cli(["data", "traded", wallet])
            unique_markets = int(traded.get("traded", traded.get("markets_traded", 0))) if isinstance(traded, dict) else 0
        except Exception:
            unique_markets = 0

        try:
            trades = run_cli(["data", "trades", wallet, "--limit", str(args.trades_limit)])
            if not isinstance(trades, list):
                trades = []
        except Exception:
            trades = []

        trades = filter_trades_by_lookback_hours(trades, args.lookback_hours)

        rep = wallet_score(
            wallet=wallet,
            username=username,
            trades=trades,
            unique_markets_traded=unique_markets,
            mcache=mcache,
            big_bet_usd=args.big_bet,
            pre_end_hours_min=args.pre_end_hours_min,
            pre_end_hours_max=args.pre_end_hours_max,
        )
        reports.append(rep)
        wallet_trades[wallet] = trades

        if args.sleep_ms > 0:
            time.sleep(args.sleep_ms / 1000)
        if i % 10 == 0:
            print(f"scanned {i}/{len(board)} wallets...", file=sys.stderr)

    reports.sort(key=lambda r: r.score, reverse=True)

    rows: List[Dict[str, Any]] = []
    new_state_wallets: Dict[str, Any] = {}

    for r in reports:
        # Simplified: no historical data used for scoring
        delta_score = 0  # Always 0 since we don't track history
        tier = classify_tier(r.score)

        recent_actions = summarize_recent_actions(
            wallet_trades.get(r.wallet, []),
            limit=5,
            min_size=args.action_min_size,
            side_filter=args.action_side,
            lookback_hours=args.action_lookback_hours,
        )
        wr = fetch_wallet_winrate(r.wallet, closed_limit=args.closed_limit)
        row = {
            "wallet": r.wallet,
            "username": r.username,
            "score": r.score,
            "delta_score": delta_score,
            "tier": tier,
            "reasons": r.reasons,
            "metrics": r.metrics,
            "win_rate": wr.get("win_rate", 0.0),
            "closed_count": wr.get("closed_count", 0),
            "recent_actions": recent_actions,
        }
        rows.append(row)
        # Still save state for debugging but don't use for scoring next time
        new_state_wallets[r.wallet] = {
            "username": r.username,
            "score": r.score,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    suspects = [x for x in rows if x["score"] >= args.min_score]

    print(f"\n=== Insider-style suspect wallets (score >= {args.min_score}) ===")
    for x in suspects[: args.top]:
        print(f"\n{x['wallet']}  ({x['username']})")
        print(f"score: {x['score']}  tier: {x['tier']}")
        print("reasons: " + "; ".join(x["reasons"][:5]))
        m = x["metrics"]
        print(
            "metrics: "
            f"trades={m['trade_count']}, unique={m['unique_markets']}, "
            f"median=${m['median_size']}, max=${m['max_size']}, "
            f"focus={m['focus_ratio']:.2f}, timing={m['timing_ratio']:.2f}"
        )
        if x.get("recent_actions"):
            print("recent actions:")
            for a in x["recent_actions"][:3]:
                print(
                    f"  - {a['time']} | {a['side']} ${a['size']:.2f} @ {a['price']:.4f} | {a['outcome']} | {a['slug']}"
                )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": vars(args),
        "suspects": rows,
        "summary": {
            "total_scanned": len(rows),
            "crossed_threshold": len(suspects),
            "core": sum(1 for x in rows if x["tier"] == "core"),
            "candidate": sum(1 for x in rows if x["tier"] == "candidate"),
            "remove": sum(1 for x in rows if x["tier"] == "remove"),
        },
    }

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nSaved full result to: {args.out}")

    if args.report_md:
        md = build_report_md("Polymarket Insider Tracker Report", payload["generated_at"], rows[: args.top])
        with open(args.report_md, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"Saved markdown report to: {args.report_md}")

    save_state(
        args.state_file,
        {
            "generated_at": payload["generated_at"],
            "wallets": new_state_wallets,
            "last_summary": payload["summary"],
        },
    )

    if not suspects:
        print("\nNo wallets crossed threshold. Lower --min-score or widen sample.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
