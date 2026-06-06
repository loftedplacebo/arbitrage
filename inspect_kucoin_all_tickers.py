from Kucoin.kucoin_market_adapter import KucoinMarketAdapter
import json

adapter = KucoinMarketAdapter()
data = adapter.get_futures_all_tickers()

print(type(data), len(data) if isinstance(data, list) else "not list")
print(json.dumps(data[0], indent=2) if data else "NO DATA")