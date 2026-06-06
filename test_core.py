from datetime import datetime, timezone

from core.models import OrderBook, OrderBookLevel
from core.orderbook import estimate_execution_from_orderbook
from core.scoring import calculate_spot_futures_basis_pct, calculate_net_edge_pct
from core.symbols import normalise_symbol, standard_to_exchange_symbol


print("Symbol tests")
print(normalise_symbol("BTC_USDT"))
print(standard_to_exchange_symbol("BTCUSDT", "mexc", "futures"))
print(standard_to_exchange_symbol("BTCUSDT", "kucoin", "futures"))

book = OrderBook(
    exchange="binance",
    market_type="spot",
    standard_symbol="BTCUSDT",
    exchange_symbol="BTCUSDT",
    bids=[
        OrderBookLevel(price=9990, quantity=1),
        OrderBookLevel(price=9980, quantity=2),
    ],
    asks=[
        OrderBookLevel(price=10000, quantity=1),
        OrderBookLevel(price=10010, quantity=2),
    ],
    observed_at_utc=datetime.now(timezone.utc),
)

estimate = estimate_execution_from_orderbook(book, side="buy", notional_usdt=15000)
print("\nExecution estimate")
print(estimate)

basis = calculate_spot_futures_basis_pct(spot_ask=10000, futures_bid=10050)
net = calculate_net_edge_pct(
    gross_spread_pct=basis,
    estimated_fees_pct=0.12,
    estimated_slippage_pct=0.03,
    expected_funding_pct=0.02,
)

print("\nScoring")
print("basis", basis)
print("net", net)