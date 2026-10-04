

class PositionSizer:
    """Dynamic position sizing using a Kelly Criterion variation combined with GEX risk."""

    def __init__(self, base_win_rate: float = 0.55, win_loss_ratio: float = 2.0, half_kelly: bool = True):
        self.base_win_rate = base_win_rate
        self.win_loss_ratio = win_loss_ratio
        self.half_kelly = half_kelly

    def calculate_kelly_fraction(self) -> float:
        """Calculate the Kelly Criterion percentage."""
        if self.win_loss_ratio <= 0:
            return 0.0

        kelly = self.base_win_rate - ((1 - self.base_win_rate) / self.win_loss_ratio)
        if kelly <= 0:
            return 0.0

        return kelly / 2.0 if self.half_kelly else kelly

    def get_position_size(self, account_balance_usdt: float, gex_modifier: float = 1.0) -> float:
        """
        Calculate the fixed USDT amount to risk based on account balance, Kelly fraction, and GEX condition.
        gex_modifier: 1.0 for neutral/favourable GEX, < 1.0 for unfavourable GEX.
        """
        kelly_fraction = self.calculate_kelly_fraction()

        # Max risk is capped at 5% of account balance even with Kelly
        max_risk_fraction = min(kelly_fraction, 0.05)

        # Adjust by GEX condition
        adjusted_risk_fraction = max_risk_fraction * gex_modifier

        return max(0.0, account_balance_usdt * adjusted_risk_fraction)
