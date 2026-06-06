# config.py

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(exist_ok=True)

KUCOIN_FUTURES_BASE_URL = "https://api-futures.kucoin.com"

SCAN_INTERVAL_SECONDS = 60

DATABASE_PATH = str(DATA_DIR / "kucoin_scanner.db")

MAX_SYMBOLS_TO_SCAN = 600

TAKER_FEE_PCT = 0.06
MAKER_FEE_PCT = 0.02

# Only fetch order books for the strongest funding candidates
MAX_ORDERBOOK_SYMBOLS = 20

# Optional early liquidity/quality filters
MIN_TURNOVER_24H_USDT = 1_000_000