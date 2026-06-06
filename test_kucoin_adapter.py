from Kucoin.kucoin_market_adapter import KucoinMarketAdapter
from core.orderbook import estimate_execution_from_orderbook


adapter = KucoinMarketAdapter()

symbol = "BTCUSDT"
notional = 1000

book = adapter.get_futures_orderbook(symbol, limit=100)
funding = adapter.get_funding_info(symbol)
symbols = adapter.get_liquidity_ranked_futures_symbols(max_symbols=10)

buy_estimate = estimate_execution_from_orderbook(
    orderbook=book,
    side="buy",
    notional_usdt=notional,
)

sell_estimate = estimate_execution_from_orderbook(
    orderbook=book,
    side="sell",
    notional_usdt=notional,
)

print("\nKuCoin futures adapter test")
print("---------------------------")
print(f"Symbol:                  {symbol}")
print(f"Exchange symbol:          {book.exchange_symbol}")
print(f"Best bid:                 {book.bids[0].price if book.bids else None}")
print(f"Best ask:                 {book.asks[0].price if book.asks else None}")
print(f"Buy avg price:            {buy_estimate.average_price}")
print(f"Sell avg price:           {sell_estimate.average_price}")
print(f"Buy fillable:             {buy_estimate.is_fillable}")
print(f"Sell fillable:            {sell_estimate.is_fillable}")
print(f"Funding rate:             {funding.funding_rate}")
print(f"Funding rate %:           {(funding.funding_rate or 0) * 100:.4f}")
print(f"Next funding UTC:         {funding.next_funding_time_utc}")
print(f"Funding interval hours:   {funding.funding_interval_hours}")
print(f"Top 10 liquid symbols:    {symbols}")