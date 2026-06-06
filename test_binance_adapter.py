from Binance.binance_market_adapter import BinanceMarketAdapter
from core.orderbook import estimate_execution_from_orderbook
from core.scoring import (
    calculate_spot_futures_basis_pct,
    calculate_net_edge_pct,
)


adapter = BinanceMarketAdapter()

symbol = "BTCUSDT"
notional = 1000

spot_book = adapter.get_spot_orderbook(symbol, limit=100)
futures_book = adapter.get_futures_orderbook(symbol, limit=100)
funding = adapter.get_funding_info(symbol)

spot_buy = estimate_execution_from_orderbook(
    orderbook=spot_book,
    side="buy",
    notional_usdt=notional,
)

futures_sell = estimate_execution_from_orderbook(
    orderbook=futures_book,
    side="sell",
    notional_usdt=notional,
)

gross_basis_pct = calculate_spot_futures_basis_pct(
    spot_ask=spot_buy.average_price,
    futures_bid=futures_sell.average_price,
)

estimated_fees_pct = 0.10
estimated_slippage_pct = spot_buy.slippage_pct + futures_sell.slippage_pct
expected_funding_pct = (funding.funding_rate or 0) * 100

net_edge_pct = calculate_net_edge_pct(
    gross_spread_pct=gross_basis_pct,
    estimated_fees_pct=estimated_fees_pct,
    estimated_slippage_pct=estimated_slippage_pct,
    expected_funding_pct=expected_funding_pct,
)

print("\nBinance spot-futures basis test")
print("--------------------------------")
print(f"Symbol:                {symbol}")
print(f"Notional:              ${notional:,.0f}")
print(f"Spot avg buy:          {spot_buy.average_price}")
print(f"Futures avg sell:      {futures_sell.average_price}")
print(f"Spot fillable:         {spot_buy.is_fillable}")
print(f"Futures fillable:      {futures_sell.is_fillable}")
print(f"Gross basis %:         {gross_basis_pct:.4f}")
print(f"Funding rate %:        {expected_funding_pct:.4f}")
print(f"Slippage %:            {estimated_slippage_pct:.4f}")
print(f"Fees %:                {estimated_fees_pct:.4f}")
print(f"Net edge %:            {net_edge_pct:.4f}")
print(f"Next funding UTC:      {funding.next_funding_time_utc}")