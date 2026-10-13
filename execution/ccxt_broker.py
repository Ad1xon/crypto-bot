from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from typing import Any, Final

import ccxt.async_support as ccxt

from execution.discord_alerter import DiscordAlerter
from execution.interfaces import ExecutionEngine
from quant_models.position_sizer import PositionSizer

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

_BULLISH: Final[str] = "BULLISH_ABSORPTION"
_BEARISH: Final[str] = "BEARISH_ABSORPTION"
_GEX_HIGH_THRESHOLD: Final[float] = 0.05
_GEX_LOW_THRESHOLD: Final[float] = 0.01
_MAX_CANCEL_RETRIES: Final[int] = 3
_DEFAULT_STATE_FILE: Final[pathlib.Path] = pathlib.Path("data/live_state.json")


class CCXTBroker(ExecutionEngine):
    """Async live-exchange broker via ccxt.async_support.

    Call ``await initialize()`` before trading and ``await close()`` on shutdown.
    Position state is atomically persisted to ``data/live_state.json`` on every
    transition so restarts recover correctly. Sets ``_system_fault=True`` when
    bracket-order cancellation fails — freezing all new entries until the state
    file is manually deleted.
    """

    def __init__(
        self,
        exchange_id: str,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        symbol: str = "BTC/USDT",
        risk_per_trade_pct: float = 0.01,
        base_sl_usdt_dist: float = 150.0,
        tp_usdt_dist: float = 300.0,
        tp_pct: float | None = None,
        sl_pct: float | None = None,
        trailing_activation_pct: float | None = None,
        fee_rate: float = 0.0004,
        trailing_activation_usdt: float = 100.0,
        cooldown_s: float = 60.0,
        max_daily_loss_usdt: float = 500.0,
        position_sizer: PositionSizer | None = None,
        state_file: pathlib.Path = _DEFAULT_STATE_FILE,
    ) -> None:
        exchange_class: type = getattr(ccxt, exchange_id)

        exchange_config = {
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }

        if exchange_id == "hyperliquid":
            exchange_config["walletAddress"] = api_key
            exchange_config["privateKey"] = api_secret
        else:
            exchange_config["apiKey"] = api_key
            exchange_config["secret"] = api_secret

        self._exchange: ccxt.Exchange = exchange_class(exchange_config)
        if testnet:
            self._exchange.set_sandbox_mode(True)

        self._symbol: str = symbol
        self._risk_per_trade_pct: float = risk_per_trade_pct
        self._base_sl_usdt_dist: float = base_sl_usdt_dist
        self._tp_usdt_dist: float = tp_usdt_dist
        self._tp_pct: float | None = tp_pct
        self._sl_pct: float | None = sl_pct
        self._trailing_activation_pct: float | None = trailing_activation_pct
        self._active_sl_dist: float = base_sl_usdt_dist
        self._active_tp_dist: float = tp_usdt_dist
        self._active_activation_dist: float = trailing_activation_usdt
        self._fee_rate: float = fee_rate
        self._trailing_activation_usdt: float = trailing_activation_usdt
        self._cooldown_s: float = cooldown_s
        self._max_daily_loss_usdt: float = max_daily_loss_usdt
        self._position_sizer: PositionSizer | None = position_sizer
        self._state_file: pathlib.Path = state_file

        self._position: str | None = None
        self._entry_price: float = 0.0
        self._position_btc: float = 0.0
        self._current_local_sl_price: float = 0.0
        self._last_trade_time: float = 0.0
        self._session_pnl: float = 0.0
        self._system_fault: bool = False
        self._startup_fault_alert: bool = False

        self._alerter: DiscordAlerter = DiscordAlerter()

        self._load_state()

    async def initialize(self) -> None:
        """Load exchange markets. Must be awaited once before any trading call.

        Also dispatches a deferred Discord alert when the broker rebooted into a
        pre-existing system-fault state — the alert cannot be sent during ``__init__``
        because no event loop is running at that point.
        """
        await self._exchange.load_markets()
        _LOG.info(
            "[CCXT BROKER] Initialized — markets loaded for %s (testnet=%s).",
            self._symbol,
            self._exchange.sandbox,
        )
        if self._startup_fault_alert:
            asyncio.create_task(
                self._alerter.send_alert(
                    title="SYSTEM FAULT: Bot Rebooted in Frozen State",
                    description=(
                        f"CCXTBroker restarted but the system-fault flag is still set from a "
                        f"prior session. All trading is frozen. "
                        f"Manually verify open orders on the exchange, then delete "
                        f"`{self._state_file}` to reset."
                    ),
                )
            )

    async def close(self) -> None:
        """Release the aiohttp session. Call on graceful shutdown."""
        await self._exchange.close()
        _LOG.info("[CCXT BROKER] Exchange HTTP session closed.")

    def _build_state_dict(self) -> dict[str, Any]:
        return {
            "position": self._position,
            "entry_price": self._entry_price,
            "position_btc": self._position_btc,
            "current_local_sl_price": self._current_local_sl_price,
            "last_trade_time": self._last_trade_time,
            "session_pnl": self._session_pnl,
            "system_fault": self._system_fault,
        }

    def _save_state(self) -> None:
        """Write position state atomically via tmp-then-rename."""
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp: pathlib.Path = self._state_file.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._build_state_dict(), fh, indent=2)
            tmp.replace(self._state_file)
        except OSError as exc:
            _LOG.error("[CCXT BROKER] Failed to persist state — %s", exc)

    def _load_state(self) -> None:
        """Restore position state from disk; writes a fresh snapshot on any failure."""
        if not self._state_file.exists():
            self._save_state()
            _LOG.info("[CCXT BROKER] No prior state file — starting fresh.")
            return

        try:
            with open(self._state_file, "r", encoding="utf-8") as fh:
                data: dict[str, Any] = json.load(fh)

            raw_pos = data.get("position")
            self._position = str(raw_pos) if raw_pos is not None else None
            self._entry_price = float(data.get("entry_price", 0.0))
            self._position_btc = float(data.get("position_btc", 0.0))
            self._current_local_sl_price = float(data.get("current_local_sl_price", 0.0))
            self._last_trade_time = float(data.get("last_trade_time", 0.0))
            self._session_pnl = float(data.get("session_pnl", 0.0))
            self._system_fault = bool(data.get("system_fault", False))

            _LOG.warning(
                "[CCXT BROKER] Restored state — "
                "Position: %s | Entry: $%.2f | SL: $%.2f | "
                "Session PnL: $%+.4f | System Fault: %s",
                self._position,
                self._entry_price,
                self._current_local_sl_price,
                self._session_pnl,
                self._system_fault,
            )
            if self._system_fault:
                _LOG.critical(
                    "[CCXT BROKER] SYSTEM FAULT flag set from prior session. "
                    "All trading frozen. Delete %s to reset after manual verification.",
                    self._state_file,
                )
                self._startup_fault_alert = True

        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            _LOG.warning("[CCXT BROKER] State file unreadable (%s) — starting fresh.", exc)
            self._save_state()

    @property
    def has_open_position(self) -> bool:
        return self._position is not None

    def is_on_cooldown(self, current_time: float) -> bool:
        return current_time - self._last_trade_time < self._cooldown_s

    async def fetch_balance(self) -> float:
        """Return free USDT balance via async REST call."""
        try:
            raw: dict = await self._exchange.fetch_balance()
            return float(raw["free"].get("USDT", 0.0))
        except ccxt.RateLimitExceeded as exc:
            _LOG.warning("[CCXT BROKER] Rate limit hit fetching balance — %s", exc)
            return 0.0
        except ccxt.BaseError as exc:
            _LOG.error("[CCXT BROKER] Failed to fetch balance — %s", exc)
            return 0.0

    def _gex_multiplier(self, abs_gex: float) -> float:
        if abs_gex >= _GEX_HIGH_THRESHOLD:
            return 1.2
        if abs_gex >= _GEX_LOW_THRESHOLD:
            return 1.0
        return 0.8

    async def update_tick(self, current_price: float, current_time: float) -> None:
        """Ratchet the trailing SL and trigger a market exit if any level is breached."""
        if self._position is None:
            return

        fee_coverage: float = self._entry_price * (self._fee_rate * 2.0)
        sl_price_delta: float = (
            self._active_sl_dist
            if self._sl_pct is not None or self._position_btc == 0.0
            else self._active_sl_dist / self._position_btc
        )
        activation_price_delta: float = (
            self._active_activation_dist
            if self._trailing_activation_pct is not None or self._sl_pct is not None or self._position_btc == 0.0
            else self._active_activation_dist / self._position_btc
        )

        exit_side: str | None = None

        if self._position == "LONG":
            true_be_price: float = self._entry_price + fee_coverage
            if current_price >= self._entry_price + activation_price_delta:
                dynamic_sl: float = current_price - sl_price_delta
                self._current_local_sl_price = max(self._current_local_sl_price, true_be_price, dynamic_sl)
            if (
                current_price >= self._entry_price + self._active_tp_dist
                or current_price <= self._current_local_sl_price
            ):
                exit_side = "sell"

        elif self._position == "SHORT":
            true_be_price = self._entry_price - fee_coverage
            if current_price <= self._entry_price - activation_price_delta:
                dynamic_sl = current_price + sl_price_delta
                self._current_local_sl_price = min(self._current_local_sl_price, true_be_price, dynamic_sl)
            if (
                current_price <= self._entry_price - self._active_tp_dist
                or current_price >= self._current_local_sl_price
            ):
                exit_side = "buy"

        if exit_side is not None:
            await self._execute_market_exit(exit_side, current_price)

    async def _execute_market_exit(self, side: str, current_price: float) -> None:
        """Place a reduceOnly market order then cancel remaining bracket orders.

        Exit-order failure leaves the position open for retry on the next tick.
        Bracket-cancel failure sets ``_system_fault`` and freezes all trading.
        """
        if self._system_fault:
            _LOG.critical("[CCXT BROKER] Exit blocked — system is in fault state. Manual intervention required.")
            return

        size: float = self._position_btc
        reduce_params: dict[str, bool] = {"reduceOnly": True}

        try:
            if side == "sell":
                await self._exchange.create_market_sell_order(self._symbol, size, params=reduce_params)
            else:
                await self._exchange.create_market_buy_order(self._symbol, size, params=reduce_params)
            _LOG.warning(
                "[CCXT BROKER] Local Trailing SL/TP hit — position closed at market. "
                "symbol: %s | side: %s | size: %.6f | approx price: %.2f",
                self._symbol,
                side,
                size,
                current_price,
            )
        except ccxt.NetworkError as exc:
            _LOG.error("[CCXT BROKER] Network error during local SL exit — %s", exc)
            return
        except ccxt.RateLimitExceeded as exc:
            _LOG.warning("[CCXT BROKER] Rate limit during local SL exit — %s", exc)
            return
        except ccxt.BaseError as exc:
            _LOG.error("[CCXT BROKER] Exchange error during local SL exit — %s", exc)
            return

        fee_cost: float = self._fee_rate * 2.0 * self._position_btc * self._entry_price
        if self._position == "LONG":
            net_pnl: float = self._position_btc * (current_price - self._entry_price) - fee_cost
        else:
            net_pnl = self._position_btc * (self._entry_price - current_price) - fee_cost
        self._session_pnl += net_pnl

        cancel_ok: bool = False
        for attempt in range(1, _MAX_CANCEL_RETRIES + 1):
            try:
                await self._exchange.cancel_all_orders(self._symbol)
                cancel_ok = True
                break
            except ccxt.NetworkError as exc:
                _LOG.warning(
                    "[CCXT BROKER] cancel_all_orders attempt %d/%d failed — %s",
                    attempt,
                    _MAX_CANCEL_RETRIES,
                    exc,
                )
                if attempt < _MAX_CANCEL_RETRIES:
                    await asyncio.sleep(1.0)
            except ccxt.BaseError as exc:
                _LOG.warning("[CCXT BROKER] Could not cancel bracket orders after exit — %s", exc)
                break

        if cancel_ok:
            self._position = None
            self._entry_price = 0.0
            self._position_btc = 0.0
            self._current_local_sl_price = 0.0
            self._last_trade_time = time.time()
            self._save_state()
            _LOG.info("[CCXT BROKER] Position cleared. Session PnL: $%+.4f", self._session_pnl)
        else:
            self._system_fault = True
            self._save_state()
            _LOG.critical(
                "[CCXT BROKER] SYSTEM FAULT: Failed to cancel bracket orders after %d retries. "
                "Orphaned orders may be live on the exchange. "
                "ALL FURTHER TRADING IS FROZEN. "
                "Manually verify and cancel open orders, then delete %s to reset.",
                _MAX_CANCEL_RETRIES,
                self._state_file,
            )
            asyncio.create_task(
                self._alerter.send_alert(
                    title="SYSTEM FAULT: Orphaned Orders Detected",
                    description=(
                        f"Failed to cancel bracket orders after {_MAX_CANCEL_RETRIES} retries. "
                        f"Orphaned orders may be live on the exchange. "
                        f"**ALL FURTHER TRADING IS FROZEN.** "
                        f"Manually verify and cancel open orders, then delete "
                        f"`{self._state_file}` to reset."
                    ),
                )
            )

    async def execute_signal(
        self,
        signal_type: str,
        current_price: float,
        current_time: float,
        gex_value: float = 0.0,
        signal_ts: float | None = None,
    ) -> None:
        """Submit a GEX-sized bracket entry order, subject to all safety guards."""
        if self._system_fault:
            _LOG.critical(
                "[CCXT BROKER] Signal %s blocked — system is in fault state. Manual intervention required.",
                signal_type,
            )
            return

        if self._session_pnl <= -self._max_daily_loss_usdt:
            _LOG.critical(
                "[CCXT BROKER] Kill-switch ACTIVE: daily loss limit reached "
                "(Session PnL: $%+.4f / Limit: -$%.2f). Signal %s BLOCKED.",
                self._session_pnl,
                self._max_daily_loss_usdt,
                signal_type,
            )
            asyncio.create_task(
                self._alerter.send_alert(
                    title="CRITICAL: Daily Loss Kill-Switch Activated",
                    description=(
                        f"Session PnL dropped to **${self._session_pnl:+.4f}** against a "
                        f"limit of **-${self._max_daily_loss_usdt:.2f}**. "
                        f"All new entries are frozen for the remainder of this session."
                    ),
                )
            )
            return

        if current_time - self._last_trade_time < self._cooldown_s:
            remaining: float = self._cooldown_s - (current_time - self._last_trade_time)
            _LOG.debug(
                "[CCXT BROKER] Cooldown active — %.1fs remaining, signal %s ignored.",
                remaining,
                signal_type,
            )
            return

        if self._position is not None:
            _LOG.debug(
                "[CCXT BROKER] Already in a %s position — signal %s ignored.",
                self._position,
                signal_type,
            )
            return

        current_balance: float = await self.fetch_balance()
        if current_balance <= 0.0:
            _LOG.error("[CCXT BROKER] Balance fetch returned zero — signal %s aborted.", signal_type)
            return

        if self._sl_pct is not None:
            effective_sl_dist: float = current_price * self._sl_pct
            effective_tp_dist: float = current_price * (
                self._tp_pct if self._tp_pct is not None else self._sl_pct * 2.0
            )
            effective_activation: float = (
                current_price * self._trailing_activation_pct
                if self._trailing_activation_pct is not None
                else effective_tp_dist / 3.0
            )
        else:
            effective_sl_dist = self._base_sl_usdt_dist
            effective_tp_dist = self._tp_usdt_dist
            effective_activation = self._trailing_activation_usdt

        self._active_sl_dist = effective_sl_dist
        self._active_tp_dist = effective_tp_dist
        self._active_activation_dist = effective_activation

        multiplier: float = self._gex_multiplier(abs(gex_value))
        if self._position_sizer is not None:
            base_size_btc: float = self._position_sizer.calculate_position_btc(
                account_balance_usdt=current_balance,
                sl_distance_usdt=effective_sl_dist,
                gex_modifier=multiplier,
            )
        else:
            risk_capital: float = current_balance * self._risk_per_trade_pct
            base_size_btc = (risk_capital / effective_sl_dist) * multiplier

        final_size_btc: float = float(self._exchange.amount_to_precision(self._symbol, base_size_btc))

        if signal_type == _BULLISH:
            hard_sl_price: float = current_price - effective_sl_dist
            hard_tp_price: float = current_price + effective_tp_dist
            direction: str = "LONG"
        elif signal_type == _BEARISH:
            hard_sl_price = current_price + effective_sl_dist
            hard_tp_price = current_price - effective_tp_dist
            direction = "SHORT"
        else:
            _LOG.warning("[CCXT BROKER] Unknown signal type '%s' — no order placed.", signal_type)
            return

        sl_price_str: str = self._exchange.price_to_precision(self._symbol, hard_sl_price)
        tp_price_str: str = self._exchange.price_to_precision(self._symbol, hard_tp_price)

        params: dict[str, str] = {
            "stopLossPrice": sl_price_str,
            "takeProfitPrice": tp_price_str,
        }

        try:
            if direction == "LONG":
                order: dict = await self._exchange.create_market_buy_order(self._symbol, final_size_btc, params=params)
            else:
                order = await self._exchange.create_market_sell_order(self._symbol, final_size_btc, params=params)

            fill_ts: float = time.perf_counter()
            latency_ms: float | None = None
            if signal_ts is not None:
                latency_ms = (fill_ts - signal_ts) * 1000.0
                _LOG.info("Order executed. Latency: %.2f ms", latency_ms)

            order_id: str = str(order.get("id", "n/a"))
            _LOG.warning(
                "[CCXT BROKER] %s order placed — symbol: %s | id: %s | "
                "size: %.6f BTC | GEX mult: %.1fx | hard SL: %s | hard TP: %s",
                direction,
                self._symbol,
                order_id,
                final_size_btc,
                multiplier,
                sl_price_str,
                tp_price_str,
            )

            self._position = direction
            self._entry_price = current_price
            self._position_btc = final_size_btc
            self._current_local_sl_price = float(sl_price_str)
            self._save_state()

            latency_field: str = f"{latency_ms:.2f} ms" if latency_ms is not None else "n/a"
            asyncio.create_task(
                self._alerter.send_alert(
                    title=f"Order Executed — {direction}",
                    description=(
                        f"**{signal_type}** filled on `{self._symbol}`.\n"
                        f"Size: `{final_size_btc:.6f}` BTC @ ~`${current_price:,.2f}`\n"
                        f"Order ID: `{order_id}`"
                    ),
                    color=0x2ECC71,
                    fields=[
                        {
                            "name": "Latency",
                            "value": latency_field,
                            "inline": True,
                        },
                        {
                            "name": "GEX Mult",
                            "value": f"{multiplier:.1f}x",
                            "inline": True,
                        },
                        {
                            "name": "SL / TP",
                            "value": f"`{sl_price_str}` / `{tp_price_str}`",
                            "inline": True,
                        },
                    ],
                )
            )

        except ccxt.NetworkError as exc:
            _LOG.error("[CCXT BROKER] Network error executing signal '%s' — %s", signal_type, exc)
        except ccxt.RateLimitExceeded as exc:
            _LOG.warning("[CCXT BROKER] Rate limit executing signal '%s' — %s", signal_type, exc)
        except ccxt.BaseError as exc:
            _LOG.error("[CCXT BROKER] Exchange error executing signal '%s' — %s", signal_type, exc)
