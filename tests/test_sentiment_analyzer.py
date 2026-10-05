from unittest.mock import MagicMock, patch

import pytest

from quant_models.sentiment_analyzer import CryptoSentimentAnalyzer


@pytest.mark.asyncio
async def test_sentiment_analyzer_logic():
    analyzer = CryptoSentimentAnalyzer(feeds=["mock_feed"])

    # Mock RSS fetcher
    with patch.object(analyzer, "_fetch_rss_sync") as mock_rss:
        mock_rss.return_value = [
            "Bitcoin surge leads to massive rally",
            "Crypto crash and massive dump incoming",
            "Neutral news about blockchain",
        ]

        # Mock FinBERT pipeline output
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = [
            {"label": "positive", "score": 0.9},
            {"label": "negative", "score": 0.8},
            {"label": "neutral", "score": 0.95},
        ]

        # Patch the init_model to inject our mocked pipeline
        with patch.object(analyzer, "_init_model"):
            analyzer._nlp_pipeline = mock_pipeline

            score = await analyzer.update_sentiment()

            assert score.total_articles_parsed == 3
            assert score.bullish_mentions == 1
            assert score.bearish_mentions == 1
            # net score = 0.9 (bullish) - 0.8 (bearish) = 0.1
            assert abs(score.net_score - 0.1) < 1e-5

            assert analyzer.current_score == score
