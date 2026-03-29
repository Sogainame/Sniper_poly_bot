"""
Polymarket Smart Money Longshot Scanner
=======================================
Сканирует ВСЕ активные рынки Polymarket, находит токены по $0.01-$0.10
с аномальным объёмом — потенциальный сигнал "smart money".

Логика:
1. Gamma API → загружаем все активные рынки (пагинация)
2. Фильтруем: хотя бы один outcome ≤ MAX_TOKEN_PRICE
3. Для каждого лонгшота считаем anomaly score:
   - volume_24h / liquidity (агрессивная торговля)
   - volume_24h / total_volume (свежий всплеск)
   - order book: крупные bid'ы на дешёвых токенах
4. Ранжируем, выводим, шлём Telegram алерты

Запуск:
  python longshot_scanner.py                    # однократный скан
  python longshot_scanner.py --loop 300         # каждые 5 мин
  python longshot_scanner.py --telegram         # с алертами
  python longshot_scanner.py --max-price 0.15   # расширить диапазон
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

import config
from notifier import send_telegram

# ─── Config ───────────────────────────────────────────────────────────────────

GAMMA_URL = config.GAMMA_API
CLOB_URL = config.CLOB_HOST

# Defaults — overridden by CLI args
DEFAULT_MAX_TOKEN_PRICE = 0.10
DEFAULT_MIN_VOLUME_24H = 500
DEFAULT_MIN_LIQUIDITY = 100
DEFAULT_ALERT_THRESHOLD = 0.30
DEFAULT_TOP_N = 20

# Anomaly score weights
W_VOL_LIQ = 0.40       # volume/liquidity ratio
W_VOL_AGE = 0.30       # 24h vol / total vol freshness
W_ORDERBOOK = 0.30     # bid depth on cheap tokens

# "Big bid" threshold USD
BIG_BID_USD = 200

# Seen alerts cache — avoid spamming same market
SEEN_CACHE_FILE = Path("data/longshot_seen.json")


# ─── Data ─────────────────────────────────────────────────────────────────────

@dataclass
class LongshotToken:
    token_id: str
    outcome_name: str
    price: float
    bid_depth_usd: float = 0.0
    big_bids_count: int = 0
    best_bid: float = 0.0
    best_ask: float = 0.0


@dataclass
class LongshotMarket:
    market_id: str
    question: str
    slug: str
    category: str
    end_date: str
    volume_total: float
    volume_24h: float
    liquidity: float
    tokens: list[LongshotToken] = field(default_factory=list)
    anomaly_score: float = 0.0

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}" if self.slug else ""

    def compute_anomaly_score(self) -> None:
        # 1. Vol/Liq ratio → [0, 1]
        vol_liq = self.volume_24h / self.liquidity if self.liquidity > 0 else 0
        s_vol_liq = min(vol_liq / 10.0, 1.0)

        # 2. 24h freshness → [0, 1]
        s_fresh = min(self.volume_24h / self.volume_total, 1.0) if self.volume_total > 0 else 0

        # 3. Order book signal
        max_depth = max((t.bid_depth_usd for t in self.tokens), default=0)
        max_bigs = max((t.big_bids_count for t in self.tokens), default=0)
        s_ob = min(max_depth / 2000.0, 1.0)
        s_ob = min(s_ob + max_bigs * 0.1, 1.0)

        self.anomaly_score = s_vol_liq * W_VOL_LIQ + s_fresh * W_VOL_AGE + s_ob * W_ORDERBOOK


# ─── Seen cache ───────────────────────────────────────────────────────────────

def load_seen() -> dict[str, float]:
    """Load seen market_ids with their last alert timestamp."""
    if SEEN_CACHE_FILE.exists():
        try:
            return json.loads(SEEN_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_seen(seen: dict[str, float]) -> None:
    SEEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_CACHE_FILE.write_text(json.dumps(seen))


def is_new_alert(market_id: str, seen: dict[str, float], cooldown_secs: float = 3600) -> bool:
    """True if we haven't alerted on this market in the last cooldown_secs."""
    last = seen.get(market_id, 0)
    return (time.time() - last) > cooldown_secs


# ─── API ──────────────────────────────────────────────────────────────────────

async def fetch_all_markets(client: httpx.AsyncClient) -> list[dict]:
    """Загружаем ВСЕ активные рынки через Gamma API пагинацией."""
    all_markets: list[dict] = []
    offset = 0
    limit = 100

    while True:
        resp = await client.get(
            f"{GAMMA_URL}/markets",
            params={"closed": "false", "active": "true", "archived": "false",
                    "limit": limit, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_markets.extend(batch)
        offset += limit
        if len(batch) < limit:
            break
        await asyncio.sleep(0.15)

    return all_markets


def filter_longshots(
    raw: list[dict],
    max_price: float,
    min_vol_24h: float,
    min_liq: float,
) -> list[LongshotMarket]:
    """Оставляем рынки с дешёвыми outcomes и достаточным объёмом."""
    results: list[LongshotMarket] = []

    for m in raw:
        prices_raw = m.get("outcomePrices", "")
        outcomes_raw = m.get("outcomes", "")
        tokens_raw = m.get("clobTokenIds", "")
        if not prices_raw or not outcomes_raw or not tokens_raw:
            continue

        try:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            token_ids = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        except (json.JSONDecodeError, TypeError):
            continue

        if not (len(prices) == len(outcomes) == len(token_ids)):
            continue

        vol_total = float(m.get("volumeNum", 0) or m.get("volume", 0) or 0)
        vol_24h = float(m.get("volume24hr", 0) or 0)
        liq = float(m.get("liquidityNum", 0) or m.get("liquidity", 0) or 0)

        if vol_24h < min_vol_24h or liq < min_liq:
            continue

        cheap: list[LongshotToken] = []
        for p_str, name, tid in zip(prices, outcomes, token_ids):
            p = float(p_str)
            if 0.001 <= p <= max_price:
                cheap.append(LongshotToken(token_id=tid, outcome_name=str(name), price=p))

        if not cheap:
            continue

        results.append(LongshotMarket(
            market_id=m.get("id", ""),
            question=m.get("question", ""),
            slug=m.get("slug", ""),
            category=m.get("category", ""),
            end_date=m.get("endDateIso", m.get("endDate", "")),
            volume_total=vol_total,
            volume_24h=vol_24h,
            liquidity=liq,
            tokens=cheap,
        ))

    return results


async def enrich_orderbook(
    client: httpx.AsyncClient,
    markets: list[LongshotMarket],
    concurrency: int = 5,
) -> None:
    """Проверяем order book для каждого дешёвого токена."""
    sem = asyncio.Semaphore(concurrency)

    async def check(token: LongshotToken) -> None:
        async with sem:
            try:
                resp = await client.get(
                    f"{CLOB_URL}/book",
                    params={"token_id": token.token_id},
                    timeout=15,
                )
                if resp.status_code != 200:
                    return
                book = resp.json()

                total_usd = 0.0
                big_count = 0
                best_b = 0.0
                for bid in book.get("bids", []):
                    px = float(bid.get("price", 0))
                    sz = float(bid.get("size", 0))
                    usd = px * sz
                    total_usd += usd
                    if usd >= BIG_BID_USD:
                        big_count += 1
                    if px > best_b:
                        best_b = px

                token.bid_depth_usd = total_usd
                token.big_bids_count = big_count
                token.best_bid = best_b

                asks = book.get("asks", [])
                if asks:
                    token.best_ask = min(float(a.get("price", 999)) for a in asks)
            except Exception:
                pass
            await asyncio.sleep(0.08)

    tasks = [check(t) for m in markets for t in m.tokens]
    if tasks:
        await asyncio.gather(*tasks)


# ─── Output ───────────────────────────────────────────────────────────────────

def format_console(markets: list[LongshotMarket], top_n: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"{'='*80}",
        f"  🎯 LONGSHOT SCANNER — Smart Money Detector",
        f"  {now}",
        f"{'='*80}",
        "",
    ]
    if not markets:
        lines.append("  No longshot markets found.")
        return "\n".join(lines)

    for i, m in enumerate(markets[:top_n], 1):
        bar = "█" * int(m.anomaly_score * 20) + "░" * (20 - int(m.anomaly_score * 20))
        vol_liq = m.volume_24h / m.liquidity if m.liquidity > 0 else 0
        vol_pct = (m.volume_24h / m.volume_total * 100) if m.volume_total > 0 else 0

        lines.append(f"  #{i}  Score: {m.anomaly_score:.3f} [{bar}]")
        lines.append(f"  Q: {m.question}")
        lines.append(f"  {m.category} | Ends: {m.end_date[:10] if m.end_date else 'N/A'}")
        lines.append(f"  Vol24h: ${m.volume_24h:,.0f} | Total: ${m.volume_total:,.0f} | Liq: ${m.liquidity:,.0f}")
        lines.append(f"  Vol/Liq: {vol_liq:.2f}x | 24h/Total: {vol_pct:.1f}%")

        for t in m.tokens:
            fire = "🔥" if t.big_bids_count > 0 else "  "
            depth = f"bids: ${t.bid_depth_usd:,.0f}" if t.bid_depth_usd > 0 else "no bids"
            spread = f" | spread: ${t.best_ask - t.best_bid:.3f}" if (t.best_bid > 0 and t.best_ask > 0) else ""
            lines.append(f"  {fire} {t.outcome_name}: ${t.price:.3f} ({depth}, {t.big_bids_count} big{spread})")

        if m.url:
            lines.append(f"  🔗 {m.url}")
        lines.append(f"  {'─'*76}")

    lines.append(f"\n  Total: {len(markets)} | Showing top {min(top_n, len(markets))}")
    return "\n".join(lines)


def format_telegram_msg(m: LongshotMarket) -> str:
    vol_liq = m.volume_24h / m.liquidity if m.liquidity > 0 else 0
    vol_pct = (m.volume_24h / m.volume_total * 100) if m.volume_total > 0 else 0

    lines = [
        f"🎯 LONGSHOT ALERT | Score: {m.anomaly_score:.3f}",
        f"",
        f"{m.question}",
        f"📊 Vol24h: ${m.volume_24h:,.0f} | Liq: ${m.liquidity:,.0f}",
        f"📈 Vol/Liq: {vol_liq:.2f}x | 24h/Total: {vol_pct:.1f}%",
    ]
    for t in m.tokens:
        fire = "🔥" if t.big_bids_count > 0 else "💰"
        lines.append(f"{fire} {t.outcome_name}: ${t.price:.3f} (bids: ${t.bid_depth_usd:,.0f})")
    if m.url:
        lines.append(f"\n🔗 {m.url}")
    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def scan_once(
    *,
    max_price: float = DEFAULT_MAX_TOKEN_PRICE,
    min_vol: float = DEFAULT_MIN_VOLUME_24H,
    min_liq: float = DEFAULT_MIN_LIQUIDITY,
    top_n: int = DEFAULT_TOP_N,
    do_telegram: bool = False,
    alert_threshold: float = DEFAULT_ALERT_THRESHOLD,
) -> list[LongshotMarket]:
    """Full scan cycle."""
    async with httpx.AsyncClient() as client:
        print("  📡 Fetching all active markets...")
        raw = await fetch_all_markets(client)
        print(f"  📦 Got {len(raw)} markets")

        longshots = filter_longshots(raw, max_price, min_vol, min_liq)
        print(f"  🎯 {len(longshots)} longshots (price ≤ ${max_price})")

        if not longshots:
            print("  Nothing found.")
            return []

        total_tokens = sum(len(m.tokens) for m in longshots)
        print(f"  📖 Checking order books for {total_tokens} tokens...")
        await enrich_orderbook(client, longshots)

        for m in longshots:
            m.compute_anomaly_score()
        ranked = sorted(longshots, key=lambda x: x.anomaly_score, reverse=True)

        print(format_console(ranked, top_n))

        if do_telegram:
            seen = load_seen()
            sent = 0
            for m in ranked:
                if m.anomaly_score >= alert_threshold and is_new_alert(m.market_id, seen):
                    send_telegram(format_telegram_msg(m))
                    seen[m.market_id] = time.time()
                    sent += 1
                    time.sleep(0.3)
            if sent:
                save_seen(seen)
                print(f"  📲 Sent {sent} Telegram alerts")

        return ranked


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Longshot Scanner")
    parser.add_argument("--loop", type=int, default=0, help="Loop interval secs (0=once)")
    parser.add_argument("--telegram", action="store_true", help="Send Telegram alerts")
    parser.add_argument("--threshold", type=float, default=DEFAULT_ALERT_THRESHOLD,
                        help=f"Alert threshold (default: {DEFAULT_ALERT_THRESHOLD})")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="Top N results")
    parser.add_argument("--max-price", type=float, default=DEFAULT_MAX_TOKEN_PRICE,
                        help="Max token price for longshot")
    parser.add_argument("--min-vol", type=float, default=DEFAULT_MIN_VOLUME_24H,
                        help="Min 24h volume USD")
    parser.add_argument("--min-liq", type=float, default=DEFAULT_MIN_LIQUIDITY,
                        help="Min liquidity USD")
    args = parser.parse_args()

    kwargs = dict(
        max_price=args.max_price,
        min_vol=args.min_vol,
        min_liq=args.min_liq,
        top_n=args.top,
        do_telegram=args.telegram,
        alert_threshold=args.threshold,
    )

    if args.loop > 0:
        print(f"  🔄 Loop mode: every {args.loop}s")
        while True:
            try:
                asyncio.run(scan_once(**kwargs))
            except Exception as e:
                print(f"  ❌ Error: {e}")
            print(f"\n  ⏳ Next scan in {args.loop}s...\n")
            time.sleep(args.loop)
    else:
        asyncio.run(scan_once(**kwargs))


if __name__ == "__main__":
    main()
