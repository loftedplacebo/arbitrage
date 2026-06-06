# config.py

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(exist_ok=True)

BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"

SCAN_INTERVAL_SECONDS = 60

DATABASE_PATH = str(DATA_DIR / "binance_scanner.db")

# Binance can return all funding rates in one call.
MAX_SYMBOLS_TO_SCAN = None

# Only fetch order books for the strongest liquid candidates.
MAX_ORDERBOOK_SYMBOLS = 50

# Initial liquidity filter.
MIN_QUOTE_VOLUME_24H_USDT = 1_000_000

# Rough fee assumptions.
TAKER_FEE_PCT = 0.05
MAKER_FEE_PCT = 0.02