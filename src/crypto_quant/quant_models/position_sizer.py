from __future__ import annotations

from typing import Final

# Absolute net-GEX magnitudes (Deribit OI-weighted gamma, BTC units) separating weak,
# normal and strong dealer positioning. Carried over unchanged from the original
# CCXTBroker constants; not tuned on data.
GEX_HIGH_THRESHOLD: Final[float] = 0.05
GEX_LOW_THRESHOLD: Final[float] = 0.01


def gex_size_multiplier(gex_value: float) -> float:
    """Scale risk by nearest-strike GEX magnitude: weak 0.8x, normal 1.0x, strong 1.2x.

    Shared by the live broker, the paper trader and the backtester so all three size
    identically for the same GEX reading.
    """
    magnitude: float = abs(gex_value)
    if magnitude >= GEX_HIGH_THRESHOLD:
        return 1.2
    if magnitude >= GEX_LOW_THRESHOLD:
        return 1.0
    return 0.8


def cap_position_btc(
    size_btc: float,
    price: float,
    balance_usdt: float,
    max_order_size_btc: float | None = None,
    max_leverage: float | None = None,
) -> float:
    """Clamp a risk-based size to the hard order limit and the margin the account can support.

    Risk-based sizing divides risk capital by a tight stop distance, so the raw result can
    be many times account equity in notional. The live broker clamps it with
    ``MAX_ORDER_SIZE_BTC`` and the exchange enforces margin; paper trading and backtests
    must apply the same limits or their results are not reachable live.
    """
    capped: float = max(size_btc, 0.0)
    if max_order_size_btc is not None:
        capped = min(capped, max_order_size_btc)
    if max_leverage is not None and price > 0.0:
        capped = min(capped, max(balance_usdt, 0.0) * max_leverage / price)
    return capped


class PositionSizer:
    """Dynamic position sizing using Half-Kelly criterion combined with GEX and sentiment risk."""

    def __init__(
        self,
        base_win_rate: float = 0.55,
        win_loss_ratio: float = 2.0,
        half_kelly: bool = True,
    ) -> None:
        self.base_win_rate: float = base_win_rate
        self.win_loss_ratio: float = win_loss_ratio
        self.half_kelly: bool = half_kelly

    def calculate_kelly_fraction(self) -> float:
        """Calculate the Kelly Criterion percentage."""
        if self.win_loss_ratio <= 0.0:
            return 0.0

        kelly = self.base_win_rate - ((1.0 - self.base_win_rate) / self.win_loss_ratio)
        if kelly <= 0.0:
            return 0.0

        return kelly / 2.0 if self.half_kelly else kelly

    @staticmethod
    def calculate_sentiment_modifier(signal_type: str, net_score: float) -> float:
        """Map FinBERT net sentiment score to a dynamic risk multiplier bounded to [0.5, 1.2].

        - Aligned sentiment confirms order flow: boosts position up to 1.2x.
        - Neutral sentiment (score ~ 0): 1.0x (no adjustment).
        - Opposing sentiment flags narrative conflict: scales down size to 0.5x.
        """
        if abs(net_score) < 1e-6:
            return 1.0

        sig = signal_type.upper()
        is_bullish = "BULL" in sig or sig == "LONG"
        is_bearish = "BEAR" in sig or sig == "SHORT"

        if not is_bullish and not is_bearish:
            return 1.0

        if is_bullish:
            if net_score > 0.0:
                return min(1.2, 1.0 + net_score * 0.05)
            return max(0.5, 1.0 + net_score * 0.1)

        # Bearish signal
        if net_score < 0.0:
            return min(1.2, 1.0 + abs(net_score) * 0.05)
        return max(0.5, 1.0 - net_score * 0.1)

    def get_position_size(
        self,
        account_balance_usdt: float,
        gex_modifier: float = 1.0,
        sentiment_score: float = 0.0,
        signal_type: str | None = None,
    ) -> float:
        """Calculate USDT risk amount based on balance, Kelly fraction, GEX, and sentiment."""
        kelly_fraction = self.calculate_kelly_fraction()
        max_risk_fraction = min(kelly_fraction, 0.05)

        sentiment_modifier = (
            self.calculate_sentiment_modifier(signal_type, sentiment_score)
            if signal_type is not None and abs(sentiment_score) > 1e-6
            else 1.0
        )

        adjusted_risk_fraction = max_risk_fraction * gex_modifier * sentiment_modifier
        return max(0.0, account_balance_usdt * adjusted_risk_fraction)

    def calculate_position_btc(
        self,
        account_balance_usdt: float,
        sl_distance_usdt: float,
        gex_modifier: float = 1.0,
        sentiment_score: float = 0.0,
        signal_type: str | None = None,
    ) -> float:
        """Calculate position size in BTC based on Kelly risk capital, SL distance, GEX, and sentiment."""
        if sl_distance_usdt <= 0.0:
            return 0.0
        risk_capital = self.get_position_size(
            account_balance_usdt=account_balance_usdt,
            gex_modifier=gex_modifier,
            sentiment_score=sentiment_score,
            signal_type=signal_type,
        )
        return risk_capital / sl_distance_usdt
