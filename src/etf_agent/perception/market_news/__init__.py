"""全市場新聞補抓與接入。"""
from .contracts import MarketNewsError, seal
from .provider import MarketNewsRSSProvider
from .tools import prepare, evaluate
from .validator import validate_pair
from .service import capture_market_news, attach_market_news, save_market_news_result, backfill_market_news, apply_database_source_reviews
