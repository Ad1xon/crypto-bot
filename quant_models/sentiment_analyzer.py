import asyncio
import logging
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)

_FEEDS: Final[list[str]] = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]


@dataclass(frozen=True)
class SentimentScore:
    bullish_mentions: int
    bearish_mentions: int
    net_score: float
    total_articles_parsed: int


class CryptoSentimentAnalyzer:
    """Fetches crypto RSS feeds and performs professional NLP sentiment analysis using FinBERT."""

    def __init__(self, feeds: list[str] | None = None):
        self.feeds = feeds if feeds else _FEEDS
        self._latest_score = SentimentScore(0, 0, 0.0, 0)

        # Lazy load the heavy transformer model so it doesn't block startup unnecessarily
        self._nlp_pipeline = None

    def _init_model(self) -> None:
        """Initialize the FinBERT model."""
        if self._nlp_pipeline is None:
            logger.info("Initializing FinBERT model... (this may take a moment)")
            try:
                from transformers import pipeline

                self._nlp_pipeline = pipeline("sentiment-analysis", model="ProsusAI/finbert")
            except ImportError:
                raise ImportError("Please install transformers and torch: pip install transformers torch")

    def _fetch_rss_sync(self, url: str) -> list[str]:
        """Fetch RSS feed synchronously and extract titles."""
        titles = []
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()
                root = ET.fromstring(xml_data)
                for item in root.findall(".//item"):
                    title = item.find("title")
                    if title is not None and title.text:
                        titles.append(title.text)
        except Exception as e:
            logger.warning("Failed to fetch RSS %s: %s", url, e)
        return titles

    def _analyze_titles_sync(self, titles: list[str]) -> SentimentScore:
        """Run FinBERT inference on titles."""
        self._init_model()

        if not titles or self._nlp_pipeline is None:
            return SentimentScore(0, 0, 0.0, 0)

        # FinBERT labels: 'positive', 'negative', 'neutral'
        results = self._nlp_pipeline(titles)

        bullish_count = 0
        bearish_count = 0
        net_weighted_score = 0.0

        for res in results:
            label = res.get("label", "").lower()
            score = float(res.get("score", 0.0))

            if label == "positive":
                bullish_count += 1
                net_weighted_score += score
            elif label == "negative":
                bearish_count += 1
                net_weighted_score -= score

        return SentimentScore(
            bullish_mentions=bullish_count,
            bearish_mentions=bearish_count,
            net_score=net_weighted_score,
            total_articles_parsed=len(titles),
        )

    async def update_sentiment(self) -> SentimentScore:
        """Asynchronously update sentiment from all feeds using FinBERT."""
        all_titles = []
        for feed in self.feeds:
            titles = await asyncio.to_thread(self._fetch_rss_sync, feed)
            all_titles.extend(titles)

        # Run heavy NLP inference in thread pool to not block asyncio event loop
        score = await asyncio.to_thread(self._analyze_titles_sync, all_titles)
        self._latest_score = score

        logger.info("FinBERT Sentiment updated: %s", self._latest_score)
        return self._latest_score

    @property
    def current_score(self) -> SentimentScore:
        return self._latest_score
