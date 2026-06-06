from Binance.binance_market_adapter import BinanceMarketAdapter
from Bitget.bitget_market_adapter import BitgetMarketAdapter
from Mexc.mexc_market_adapter import MexcMarketAdapter
from Kucoin.kucoin_market_adapter import KucoinMarketAdapter


adapters = {
    "binance": BinanceMarketAdapter(),
    "bitget": BitgetMarketAdapter(),
    "mexc": MexcMarketAdapter(),
    "kucoin": KucoinMarketAdapter(),
}

for name, adapter in adapters.items():
    tickers = adapter.get_fast_futures_tickers()
    print(f"\n{name}: {len(tickers)} tickers")

    for symbol, row in list(tickers.items())[:5]:
        print(symbol, row)