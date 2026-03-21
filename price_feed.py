"""Real-time BTC price via Binance WebSocket (aggTrade stream).

Replaces REST polling with a persistent WS connection.
Latency: ~50ms vs ~2000ms for REST.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

import websocket


@dataclass
class PriceSnapshot:
    price: float = 0.0
    event_ts_ms: int = 0   # Binance event timestamp
    recv_ts_ms: int = 0    # local receive timestamp


class BinanceWsPriceFeed:
    """Single-symbol aggTrade WebSocket feed.

    Usage:
        feed = BinanceWsPriceFeed("btcusdt")
        feed.start()
        snap = feed.latest()  # non-blocking, returns last known price
        feed.stop()
    """

    WS_BASE = "wss://stream.binance.com:9443/ws"

    def __init__(self, symbol: str = "btcusdt") -> None:
        self.symbol = symbol.lower()
        self.stream = f"{self.symbol}@aggTrade"
        self._snap = PriceSnapshot()
        self._lock = threading.Lock()
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0

    def latest(self) -> PriceSnapshot:
        with self._lock:
            return PriceSnapshot(
                price=self._snap.price,
                event_ts_ms=self._snap.event_ts_ms,
                recv_ts_ms=self._snap.recv_ts_ms,
            )

    def is_stale(self, max_age_ms: int = 3000) -> bool:
        with self._lock:
            if self._snap.recv_ts_ms == 0:
                return True
            age = int(time.time() * 1000) - self._snap.recv_ts_ms
            return age > max_age_ms

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"ws-{self.symbol}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _run_loop(self) -> None:
        """Reconnect loop — restarts WS on any failure."""
        while self._running:
            try:
                self._connect()
            except Exception as exc:
                print(f"[WS] {self.symbol} error: {exc}")
            if self._running:
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    def _connect(self) -> None:
        url = f"{self.WS_BASE}/{self.stream}"
        self._ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        self._reconnect_delay = 1.0
        print(f"[WS] {self.symbol} connected")

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
            price = float(data["p"])
            event_ts = int(data["T"])
            with self._lock:
                self._snap.price = price
                self._snap.event_ts_ms = event_ts
                self._snap.recv_ts_ms = int(time.time() * 1000)
        except Exception:
            pass

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        print(f"[WS] {self.symbol} error: {error}")

    def _on_close(self, ws: websocket.WebSocketApp, code: int | None, msg: str | None) -> None:
        print(f"[WS] {self.symbol} closed (code={code})")


if __name__ == "__main__":
    feed = BinanceWsPriceFeed("btcusdt")
    feed.start()
    for _ in range(20):
        time.sleep(1)
        snap = feed.latest()
        stale = feed.is_stale()
        print(f"px={snap.price:.2f} event_ts={snap.event_ts_ms} stale={stale}")
    feed.stop()
