from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from typing import Any, Final

import ccxt.async_support as ccxt

from crypto_quant.execution.discord_alerter import DiscordAlerter
from crypto_quant.execution.interfaces import ExecutionEngine
from crypto_quant.quant_models.position_sizer import PositionSizer, gex_size_multiplier

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

_BULLISH: Final[str] = "BULLISH_ABSORPTION"
_BEARISH: Final[str] = "BEARISH_ABSORPTION"
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
        max_order_size_btc: float = 0.05,
        default_leverage: int = 5,
        slippage_tolerance_pct: float = 0.001,
        max_consecutive_errors: int = 3,
        position_sizer: PositionSizer | None = None,
        state_file: pathlib.Path = _DEFAULT_STATE_FILE,
    ) -> None:
        exchange_class: type = getattr(ccxt, exchange_id)

        exchange_config: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
                "adjustForTimeDifference": True,
                "defaultSlippage": slippage_tolerance_pct,
            },
        }

        if exchange_id == "hyperliquid":
            exchange_config["walletAddress"] = api_key
            exchange_config["privateKey"] = api_secret
            _LOG.info(
                "[CCXT BROKER] Hyperliquid Agent Wallet setup: WalletAddress=%s. "
                "Ensure private key belongs to an authorized Agent Wallet (no withdrawal permissions).",
                api_key,
            )
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
        self._max_order_size_btc: float = max_order_size_btc
        self._default_leverage: int = default_leverage
        self._slippage_tolerance_pct: float = slippage_tolerance_pct
        self._max_consecutive_errors: int = max_consecutive_errors
        self._consecutive_errors: int = 0
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

        # Synchronize time difference with exchange to avoid nonce/timestamp rejections
        if hasattr(self._exchange, "load_time_difference"):
            try:
                await self._exchange.load_time_difference()
                _LOG.info("[CCXT BROKER] Time difference synchronized with exchange.")
            except Exception as exc:
                _LOG.warning("[CCXT BROKER] Could not synchronize time difference: %s", exc)

        # Set default leverage if supported by exchange
        if hasattr(self._exchange, "set_leverage"):
            try:
                await self._exchange.set_leverage(self._default_leverage, self._symbol)
                _LOG.info("[CCXT BROKER] Leverage set to %dx for %s.", self._default_leverage, self._symbol)
            except Exception as exc:
                _LOG.warning("[CCXT BROKER] Could not configure leverage: %s", exc)

        # Reconcile position with exchange REST ground truth
        await self._reconcile_positions_with_exchange()

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

    async def _reconcile_positions_with_exchange(self) -> None:
        """Reconcile local position state with exchange ground truth via REST."""
        if not hasattr(self._exchange, "fetch_positions"):
            return
        try:
            positions: list[dict] = await self._exchange.fetch_positions([self._symbol])
            base_sym = self._symbol.split("/")[0] if "/" in self._symbol else self._symbol
            active_pos = next(
                (p for p in positions if p.get("symbol") == self._symbol or p.get("info", {}).get("coin") == base_sym),
                None,
            )
            if active_pos is None:
                return

            contracts = float(active_pos.get("contracts", 0.0) or active_pos.get("size", 0.0) or 0.0)
            pos_side = str(active_pos.get("side", "")).upper()
            if contracts == 0.0 and self._position is not None:
                _LOG.warning(
                    "[CCXT BROKER] Reconciliation mismatch: Local had %s position, but exchange has 0 contracts. Resetting local state.",
                    self._position,
                )
                self._position = None
                self._position_btc = 0.0
                self._entry_price = 0.0
                self._current_local_sl_price = 0.0
                self._save_state()
            elif contracts > 0.0:
                mapped_side = (
                    "LONG" if pos_side in {"LONG", "BUY"} else ("SHORT" if pos_side in {"SHORT", "SELL"} else None)
                )
                if mapped_side and mapped_side != self._position:
                    _LOG.warning(
                        "[CCXT BROKER] Reconciliation: Found %s position (%.6f BTC) on exchange. Syncing.",
                        mapped_side,
                        contracts,
                    )
                    self._position = mapped_side
                    self._position_btc = contracts
                    entry_px = float(active_pos.get("entryPrice", 0.0) or 0.0)
                    if entry_px > 0.0:
                        self._entry_price = entry_px
                    self._save_state()
        except Exception as exc:
            _LOG.warning("[CCXT BROKER] Position reconciliation failed: %s", exc)

    async def _cancel_all_bracket_orders(self) -> bool:
        """Cancel open orders safely across exchanges, including Hyperliquid."""
        has_dict = getattr(self._exchange, "has", {})
        if isinstance(has_dict, dict) and has_dict.get("cancelAllOrders"):
            try:
                await self._exchange.cancel_all_orders(self._symbol)
                return True
            except (ccxt.NotSupported, ccxt.OperationFailed):
                pass
            except ccxt.BaseError as exc:
                _LOG.warning("[CCXT BROKER] cancel_all_orders failed: %s", exc)

        if isinstance(has_dict, dict) and has_dict.get("fetchOpenOrders"):
            try:
                open_orders = await self._exchange.fetch_open_orders(self._symbol)
                if not open_orders:
                    return True
                order_ids = [str(o["id"]) for o in open_orders if o.get("id") is not None]
                if not order_ids:
                    return True
                if has_dict.get("cancelOrders"):
                    try:
                        await self._exchange.cancel_orders(order_ids, self._symbol)
                        return True
                    except Exception as exc:
                        _LOG.debug("[CCXT BROKER] Batch cancel_orders failed, falling back to individual: %s", exc)
                for oid in order_ids:
                    try:
                        await self._exchange.cancel_order(oid, self._symbol)
                    except Exception as exc:
                        _LOG.warning("[CCXT BROKER] Failed to cancel order %s: %s", oid, exc)
                return True
            except Exception as exc:
                _LOG.warning("[CCXT BROKER] Fallback order cancellation failed: %s", exc)
                return False
        return True

    @property
    def has_open_position(self) -> bool:
        return self._position is not None

    def is_on_cooldown(self, current_time: float) -> bool:
        return current_time - self._last_trade_time < self._cooldown_s

    async def fetch_balance(self) -> float:
        """Return free collateral balance (settle currency of the symbol) via async REST call.

        Hyperliquid perps settle in USDC (``BTC/USDC:USDC``); hardcoding USDT would
        report a zero balance and silently abort every signal.
        """
        collateral: str = self._symbol.split(":")[-1] if ":" in self._symbol else self._symbol.split("/")[-1]
        try:
            raw: dict = await self._exchange.fetch_balance()
            return float(raw["free"].get(collateral, 0.0))
        except ccxt.RateLimitExceeded as exc:
            _LOG.warning("[CCXT BROKER] Rate limit hit fetching balance — %s", exc)
            return 0.0
        except ccxt.BaseError as exc:
            _LOG.error("[CCXT BROKER] Failed to fetch balance — %s", exc)
            return 0.0

    async def update_tick(self, current_price: float, current_time: float) -> None:
        """Ratchet the trailing SL and trigger a market exit if any level is breached.

        Also evaluates real-time equity drawdown (unrealized PnL) to activate the
        emergency kill-switch if losses breach the daily threshold.
        """
        if self._position is None:
            return

        # Hard Drawdown Circuit Breaker: Realized + Unrealized Loss
        if self._position == "LONG":
            unrealized_pnl: float = self._position_btc * (current_price - self._entry_price)
        elif self._position == "SHORT":
            unrealized_pnl = self._position_btc * (self._entry_price - current_price)
        else:
            unrealized_pnl = 0.0

        total_drawdown: float = self._session_pnl + unrealized_pnl
        if total_drawdown <= -self._max_daily_loss_usdt:
            _LOG.critical(
                "[CCXT BROKER] EMERGENCY KILL-SWITCH TRIGGERED: "
                "Total drawdown ($%.2f: Session PnL $%+.2f + Unrealized $%+.2f) "
                "breached max daily limit (-$%.2f)! Closing position immediately.",
                total_drawdown,
                self._session_pnl,
                unrealized_pnl,
                self._max_daily_loss_usdt,
            )
            asyncio.create_task(
                self._alerter.send_alert(
                    title="EMERGENCY KILL-SWITCH: Daily Drawdown Limit Breached",
                    description=(
                        f"Total drawdown reached **${total_drawdown:+.2f}** "
                        f"(Limit: **-${self._max_daily_loss_usdt:.2f}**).\n"
                        f"Emergency market close executed. Trading frozen."
                    ),
                    color=0xE74C3C,
                )
            )
            emergency_exit_side: str = "sell" if self._position == "LONG" else "buy"
            await self._execute_market_exit(emergency_exit_side, current_price)
            self._system_fault = True
            self._save_state()
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
        reduce_params: dict[str, Any] = {
            "reduceOnly": True,
            "slippage": str(self._slippage_tolerance_pct),
        }

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
            cancel_ok = await self._cancel_all_bracket_orders()
            if cancel_ok:
                break
            _LOG.warning(
                "[CCXT BROKER] cancel bracket orders attempt %d/%d failed",
                attempt,
                _MAX_CANCEL_RETRIES,
            )
            if attempt < _MAX_CANCEL_RETRIES:
                await asyncio.sleep(1.0)

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
        sentiment_score: float = 0.0,
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

        multiplier: float = gex_size_multiplier(gex_value)
        if self._position_sizer is not None:
            base_size_btc: float = self._position_sizer.calculate_position_btc(
                account_balance_usdt=current_balance,
                sl_distance_usdt=effective_sl_dist,
                gex_modifier=multiplier,
                sentiment_score=sentiment_score,
                signal_type=signal_type,
            )
        else:
            sentiment_mod: float = (
                PositionSizer.calculate_sentiment_modifier(signal_type, sentiment_score)
                if abs(sentiment_score) > 1e-6
                else 1.0
            )
            risk_capital: float = current_balance * self._risk_per_trade_pct * sentiment_mod
            base_size_btc = (risk_capital / effective_sl_dist) * multiplier

        final_size_btc: float = float(self._exchange.amount_to_precision(self._symbol, base_size_btc))

        # Hard guard: clamp position size to configured MAX_ORDER_SIZE_BTC
        if final_size_btc > self._max_order_size_btc:
            _LOG.warning(
                "[CCXT BROKER] Sizer calculated %.6f BTC, exceeding MAX_ORDER_SIZE_BTC (%.6f BTC). Clamping to safety maximum.",
                final_size_btc,
                self._max_order_size_btc,
            )
            final_size_btc = self._max_order_size_btc

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

        # Dual-format bracket order parameters: supports both generic CCXT exchanges and Hyperliquid L1
        params: dict[str, Any] = {
            "slippage": str(self._slippage_tolerance_pct),
            "stopLossPrice": sl_price_str,
            "takeProfitPrice": tp_price_str,
            "stopLoss": {"triggerPrice": sl_price_str, "type": "market"},
            "takeProfit": {"triggerPrice": tp_price_str, "type": "market"},
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
            self._consecutive_errors = 0
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

        except (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.BaseError) as exc:
            self._consecutive_errors += 1
            self._last_trade_time = current_time  # Trigger cooldown immediately to prevent rapid error loops
            _LOG.error(
                "[CCXT BROKER] %s executing signal '%s' (failure %d/%d) — %s",
                type(exc).__name__,
                signal_type,
                self._consecutive_errors,
                self._max_consecutive_errors,
                exc,
            )
            if self._consecutive_errors >= self._max_consecutive_errors:
                self._system_fault = True
                self._save_state()
                _LOG.critical(
                    "[CCXT BROKER] CIRCUIT BREAKER TRIGGERED: %d consecutive execution errors. Trading FROZEN to prevent infinite loop / fee draining.",
                    self._consecutive_errors,
                )
                asyncio.create_task(
                    self._alerter.send_alert(
                        title="CIRCUIT BREAKER: Repeated Order Failures",
                        description=(
                            f"Encountered **{self._consecutive_errors}** consecutive order execution errors.\n"
                            f"Trading has been frozen to prevent runaway spam and fee draining."
                        ),
                        color=0xE74C3C,
                    )
                )
