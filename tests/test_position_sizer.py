from quant_models.position_sizer import PositionSizer


def test_kelly_fraction():
    # W = 0.55, R = 2.0 -> Kelly = 0.55 - 0.45/2.0 = 0.55 - 0.225 = 0.325. Half Kelly = 0.1625
    sizer = PositionSizer(base_win_rate=0.55, win_loss_ratio=2.0, half_kelly=True)
    assert abs(sizer.calculate_kelly_fraction() - 0.1625) < 1e-5

    # Negative expectation
    sizer = PositionSizer(base_win_rate=0.3, win_loss_ratio=1.0)
    assert sizer.calculate_kelly_fraction() == 0.0


def test_get_position_size():
    sizer = PositionSizer(base_win_rate=0.55, win_loss_ratio=2.0, half_kelly=True)

    # Kelly fraction is ~0.16. Cap is 0.05.
    # So max_risk_fraction = 0.05
    # Balance = 10000. Risk = 500.
    pos_usdt = sizer.get_position_size(account_balance_usdt=10000.0, gex_modifier=1.0)
    assert abs(pos_usdt - 500.0) < 1e-5

    # Unfavourable GEX -> cuts size in half
    pos_usdt_gex = sizer.get_position_size(account_balance_usdt=10000.0, gex_modifier=0.5)
    assert abs(pos_usdt_gex - 250.0) < 1e-5


def test_calculate_position_btc():
    sizer = PositionSizer(base_win_rate=0.55, win_loss_ratio=2.0, half_kelly=True)
    # Risk capital is 500 USDT, SL dist is 250 USDT -> size = 2.0 BTC
    size_btc = sizer.calculate_position_btc(account_balance_usdt=10000.0, sl_distance_usdt=250.0)
    assert abs(size_btc - 2.0) < 1e-5

    # If SL distance is 0, size is 0
    assert sizer.calculate_position_btc(10000.0, 0.0) == 0.0


def test_sentiment_modifier():
    # Neutral sentiment score (0.0) -> no adjustment
    assert PositionSizer.calculate_sentiment_modifier("BULLISH_ABSORPTION", 0.0) == 1.0
    assert PositionSizer.calculate_sentiment_modifier("BEARISH_ABSORPTION", 0.0) == 1.0

    # Bullish signal + positive sentiment -> boost capped at 1.2
    assert abs(PositionSizer.calculate_sentiment_modifier("BULLISH_ABSORPTION", 2.0) - 1.1) < 1e-5
    assert PositionSizer.calculate_sentiment_modifier("LONG", 10.0) == 1.2

    # Bullish signal + negative sentiment -> discount floored at 0.5
    assert abs(PositionSizer.calculate_sentiment_modifier("BULLISH_ABSORPTION", -2.0) - 0.8) < 1e-5
    assert PositionSizer.calculate_sentiment_modifier("LONG", -10.0) == 0.5

    # Bearish signal + negative sentiment -> boost capped at 1.2
    assert abs(PositionSizer.calculate_sentiment_modifier("BEARISH_ABSORPTION", -2.0) - 1.1) < 1e-5
    assert PositionSizer.calculate_sentiment_modifier("SHORT", -10.0) == 1.2

    # Bearish signal + positive sentiment -> discount floored at 0.5
    assert abs(PositionSizer.calculate_sentiment_modifier("BEARISH_ABSORPTION", 2.0) - 0.8) < 1e-5
    assert PositionSizer.calculate_sentiment_modifier("SHORT", 10.0) == 0.5

    # Unknown signal type -> neutral 1.0
    assert PositionSizer.calculate_sentiment_modifier("UNKNOWN", 5.0) == 1.0


def test_position_sizing_with_sentiment():
    sizer = PositionSizer(base_win_rate=0.55, win_loss_ratio=2.0, half_kelly=True)
    # Balance = 10,000, max risk = 500 USDT (5%).
    # Bullish signal with negative sentiment (-2.0) -> modifier 0.8 -> risk = 400 USDT
    pos_size = sizer.get_position_size(
        account_balance_usdt=10000.0,
        gex_modifier=1.0,
        sentiment_score=-2.0,
        signal_type="BULLISH_ABSORPTION",
    )
    assert abs(pos_size - 400.0) < 1e-5

    # Position BTC with SL dist = 200 USDT -> 400 / 200 = 2.0 BTC
    pos_btc = sizer.calculate_position_btc(
        account_balance_usdt=10000.0,
        sl_distance_usdt=200.0,
        gex_modifier=1.0,
        sentiment_score=-2.0,
        signal_type="BULLISH_ABSORPTION",
    )
    assert abs(pos_btc - 2.0) < 1e-5


def test_gex_size_multiplier():
    from quant_models.position_sizer import gex_size_multiplier

    assert gex_size_multiplier(0.0) == 0.8
    assert gex_size_multiplier(0.005) == 0.8
    assert gex_size_multiplier(-0.009) == 0.8
    assert gex_size_multiplier(0.01) == 1.0
    assert gex_size_multiplier(-0.03) == 1.0
    assert gex_size_multiplier(0.049) == 1.0
    assert gex_size_multiplier(0.05) == 1.2
    assert gex_size_multiplier(-0.10) == 1.2


def test_cap_position_btc():
    from quant_models.position_sizer import cap_position_btc

    # Base uncapped
    assert cap_position_btc(1.5, price=50000.0, balance_usdt=10000.0) == 1.5

    # Negative size clamped to 0
    assert cap_position_btc(-0.5, price=50000.0, balance_usdt=10000.0) == 0.0

    # Max order size cap
    assert cap_position_btc(1.5, price=50000.0, balance_usdt=10000.0, max_order_size_btc=0.05) == 0.05

    # Leverage cap: balance 10,000 * 5x = 50,000 notional / price 50,000 = 1.0 BTC
    assert cap_position_btc(2.0, price=50000.0, balance_usdt=10000.0, max_leverage=5.0) == 1.0

    # Both caps applied, tighter wins
    assert cap_position_btc(2.0, price=50000.0, balance_usdt=10000.0, max_order_size_btc=0.05, max_leverage=5.0) == 0.05
