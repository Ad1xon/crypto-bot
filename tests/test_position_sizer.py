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
