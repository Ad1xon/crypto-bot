import math
from datetime import UTC, datetime

from crypto_quant.quant_models.gex_calculator import (
    bs_gamma,
    compute_gex,
    norm_pdf,
    parse_deribit_instrument,
)


def test_norm_pdf_properties():
    # At zero, pdf is 1 / sqrt(2*pi) ~= 0.39894228
    expected_zero = 1.0 / math.sqrt(2.0 * math.pi)
    assert math.isclose(norm_pdf(0.0), expected_zero, rel_tol=1e-7)

    # Symmetry: pdf(x) == pdf(-x)
    assert math.isclose(norm_pdf(1.5), norm_pdf(-1.5), rel_tol=1e-9)
    assert math.isclose(norm_pdf(2.58), norm_pdf(-2.58), rel_tol=1e-9)

    # Tails decay towards zero
    assert norm_pdf(10.0) < 1e-15
    assert norm_pdf(-10.0) < 1e-15


def test_bs_gamma_invalid_inputs():
    # Zero or negative underlying price
    assert bs_gamma(spot=0.0, strike=60000.0, tau=0.1, sigma=0.5) == 0.0
    assert bs_gamma(spot=-50000.0, strike=60000.0, tau=0.1, sigma=0.5) == 0.0

    # Zero or negative strike
    assert bs_gamma(spot=60000.0, strike=0.0, tau=0.1, sigma=0.5) == 0.0
    assert bs_gamma(spot=60000.0, strike=-60000.0, tau=0.1, sigma=0.5) == 0.0

    # Zero or negative time to expiry
    assert bs_gamma(spot=60000.0, strike=60000.0, tau=0.0, sigma=0.5) == 0.0
    assert bs_gamma(spot=60000.0, strike=60000.0, tau=-0.1, sigma=0.5) == 0.0

    # Zero or negative volatility
    assert bs_gamma(spot=60000.0, strike=60000.0, tau=0.1, sigma=0.0) == 0.0
    assert bs_gamma(spot=60000.0, strike=60000.0, tau=0.1, sigma=-0.5) == 0.0


def test_bs_gamma_behavior():
    spot = 65000.0
    tau = 30.0 / 365.25  # ~1 month
    sigma = 0.55  # 55% IV

    # ATM option gamma must be strictly positive
    atm_gamma = bs_gamma(spot=spot, strike=65000.0, tau=tau, sigma=sigma)
    assert atm_gamma > 0.0

    # Gamma decays as strike moves far OTM or ITM
    otm_gamma = bs_gamma(spot=spot, strike=120000.0, tau=tau, sigma=sigma)
    itm_gamma = bs_gamma(spot=spot, strike=25000.0, tau=tau, sigma=sigma)
    assert atm_gamma > otm_gamma
    assert atm_gamma > itm_gamma

    # As tau -> 0 for ATM, gamma concentrates (increases)
    short_tau_gamma = bs_gamma(spot=spot, strike=65000.0, tau=1.0 / 365.25, sigma=sigma)
    assert short_tau_gamma > atm_gamma


def test_parse_deribit_instrument_valid():
    res = parse_deribit_instrument("BTC-27DEC24-100000-C")
    assert res is not None
    strike, opt_type, expiry = res
    assert strike == 100000.0
    assert opt_type == "C"
    assert expiry == datetime(2024, 12, 27, 8, 0, 0, tzinfo=UTC)

    res_put = parse_deribit_instrument("BTC-1NOV24-65000-P")
    assert res_put is not None
    strike, opt_type, expiry = res_put
    assert strike == 65000.0
    assert opt_type == "P"
    assert expiry == datetime(2024, 11, 1, 8, 0, 0, tzinfo=UTC)


def test_parse_deribit_instrument_invalid():
    # Wrong parts count
    assert parse_deribit_instrument("BTC-PERPETUAL") is None
    assert parse_deribit_instrument("BTC-27DEC24-100000") is None
    assert parse_deribit_instrument("BTC-27DEC24-100000-C-EXTRA") is None

    # Invalid option type
    assert parse_deribit_instrument("BTC-27DEC24-100000-X") is None

    # Invalid date
    assert parse_deribit_instrument("BTC-99XYZ99-100000-C") is None

    # Non-numeric strike
    assert parse_deribit_instrument("BTC-27DEC24-STRIKE-C") is None


def test_compute_gex_empty():
    gex, spot = compute_gex([])
    assert gex == {}
    assert spot == 0.0


def test_compute_gex_aggregation():
    # Reference date: 2024-12-01 00:00:00 UTC
    ref_time = datetime(2024, 12, 1, 0, 0, 0, tzinfo=UTC)
    spot_price = 90000.0

    summaries = [
        # Call at 90,000 strike (expires 2024-12-27)
        {
            "instrument_name": "BTC-27DEC24-90000-C",
            "open_interest": 100.0,
            "mark_iv": 60.0,
            "underlying_price": spot_price,
        },
        # Put at 90,000 strike (same expiry, same OI, same IV)
        {
            "instrument_name": "BTC-27DEC24-90000-P",
            "open_interest": 100.0,
            "mark_iv": 60.0,
            "underlying_price": spot_price,
        },
        # Call at 100,000 strike
        {
            "instrument_name": "BTC-27DEC24-100000-C",
            "open_interest": 50.0,
            "mark_iv": 65.0,
            "underlying_price": spot_price,
        },
        # Put at 80,000 strike
        {
            "instrument_name": "BTC-27DEC24-80000-P",
            "open_interest": 80.0,
            "mark_iv": 55.0,
            "underlying_price": spot_price,
        },
    ]

    gex_map, extracted_spot = compute_gex(summaries, now_utc=ref_time)

    assert extracted_spot == spot_price
    assert "90000" in gex_map
    assert "100000" in gex_map
    assert "80000" in gex_map

    # Identical Call and Put at 90,000 strike cancel each other out to ~0.0
    assert math.isclose(gex_map["90000"], 0.0, abs_tol=1e-12)

    # 100,000 strike is pure Call OI -> positive GEX
    assert gex_map["100000"] > 0.0

    # 80,000 strike is pure Put OI -> negative GEX
    assert gex_map["80000"] < 0.0


def test_compute_gex_filters_expired_and_invalid():
    ref_time = datetime(2024, 12, 1, 0, 0, 0, tzinfo=UTC)

    summaries = [
        # Expired option (expiry was November 2024, before ref_time)
        {
            "instrument_name": "BTC-1NOV24-90000-C",
            "open_interest": 100.0,
            "mark_iv": 60.0,
            "underlying_price": 90000.0,
        },
        # Zero open interest
        {
            "instrument_name": "BTC-27DEC24-95000-C",
            "open_interest": 0.0,
            "mark_iv": 60.0,
            "underlying_price": 90000.0,
        },
        # Missing mark IV
        {
            "instrument_name": "BTC-27DEC24-96000-C",
            "open_interest": 10.0,
            "mark_iv": None,
            "underlying_price": 90000.0,
        },
        # Invalid instrument name
        {
            "instrument_name": "BTC-PERPETUAL",
            "open_interest": 10.0,
            "mark_iv": 50.0,
            "underlying_price": 90000.0,
        },
    ]

    gex_map, spot = compute_gex(summaries, now_utc=ref_time)
    assert gex_map == {}
    assert spot == 90000.0


def test_compute_gex_no_valid_spot():
    summaries = [
        {
            "instrument_name": "BTC-27DEC24-90000-C",
            "open_interest": 100.0,
            "mark_iv": 60.0,
            "underlying_price": 0.0,
        }
    ]
    gex_map, spot = compute_gex(summaries)
    assert gex_map == {}
    assert spot == 0.0
