import json
import pathlib

from execution.signal_logger import SignalAuditLogger


def test_signal_audit_logger(tmp_path: pathlib.Path):
    log_file = tmp_path / "test_signals.jsonl"
    logger = SignalAuditLogger(output_path=log_file)

    # 1. Log an approved signal
    sig_event = logger.log_signal(
        timestamp_ms=1782907200000,
        signal_type="BULLISH_ABSORPTION",
        price=60000.0,
        vwap=59950.0,
        gex_value=0.05,
        nearest_strike=60000.0,
        verdict="APPROVED",
    )
    assert sig_event.verdict == "APPROVED"

    # 2. Log a rejected signal (e.g. macro blackout)
    logger.log_signal(
        timestamp_ms=1782907210000,
        signal_type="BEARISH_ABSORPTION",
        price=60050.0,
        vwap=59950.0,
        gex_value=-0.02,
        nearest_strike=60000.0,
        verdict="REJECTED",
        rejection_reason="MACRO_BLACKOUT",
    )

    # 3. Log a trade entry
    logger.log_trade_entry(
        timestamp_ms=1782907200500,
        direction="LONG",
        entry_price=60000.0,
        size_btc=0.5,
        balance_after=9998.0,
    )

    # 4. Log a trade exit
    logger.log_trade_exit(
        timestamp_ms=1782907260000,
        direction="LONG",
        entry_price=60000.0,
        exit_price=60300.0,
        size_btc=0.5,
        outcome="TP",
        net_pnl=148.0,
        fees=4.0,
        balance_after=10146.0,
    )

    # Verify JSONL lines on disk
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4

    records = [json.loads(line) for line in lines]
    assert records[0]["event_type"] == "SIGNAL"
    assert records[0]["verdict"] == "APPROVED"
    assert records[1]["rejection_reason"] == "MACRO_BLACKOUT"
    assert records[2]["event_type"] == "TRADE_ENTRY"
    assert records[3]["event_type"] == "TRADE_EXIT"
    assert records[3]["outcome"] == "TP"
    assert records[3]["net_pnl"] == 148.0
