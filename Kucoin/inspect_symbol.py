# inspect_symbol.py

import json
from datetime import datetime, timezone

from kucoin_client import KuCoinFuturesClient


SYMBOL_TO_INSPECT = "SOLUSDTM"


def ms_to_datetime(ms_value):
    if ms_value is None:
        return None

    try:
        return datetime.fromtimestamp(float(ms_value) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def main():
    client = KuCoinFuturesClient()

    contracts = client.get_active_contracts()

    matching = [
        contract for contract in contracts
        if contract.get("symbol") == SYMBOL_TO_INSPECT
    ]

    if not matching:
        print(f"Symbol not found: {SYMBOL_TO_INSPECT}")
        return

    contract = matching[0]

    print("\n==============================")
    print(f"RAW CONTRACT DATA: {SYMBOL_TO_INSPECT}")
    print("==============================")
    print(json.dumps(contract, indent=4))

    print("\n==============================")
    print("FIELD CHECK")
    print("==============================")

    interesting_fields = [
        "symbol",
        "baseCurrency",
        "quoteCurrency",
        "status",
        "type",
        "markPrice",
        "indexPrice",
        "lastTradePrice",
        "fundingFeeRate",
        "predictedFundingFeeRate",
        "nextFundingRateTime",
        "nextFundingTime",
        "fundingRateSymbol",
        "maxLeverage",
        "volumeOf24h",
        "turnoverOf24h",
        "openInterest",
        "tickSize",
        "lotSize",
        "multiplier",
    ]

    for field in interesting_fields:
        value = contract.get(field)
        print(f"{field:<28} {value}")

    possible_funding_times = [
        "nextFundingRateTime",
        "nextFundingTime",
        "fundingTime",
    ]

    print("\n==============================")
    print("FUNDING TIME CHECK")
    print("==============================")

    for field in possible_funding_times:
        value = contract.get(field)
        print(f"{field:<28} raw={value} utc={ms_to_datetime(value)}")

    print("\n==============================")
    print(f"ORDER BOOK SNAPSHOT: {SYMBOL_TO_INSPECT}")
    print("==============================")

    orderbook = client.get_orderbook_snapshot(SYMBOL_TO_INSPECT)

  ##  print(json.dumps(orderbook, indent=4))

 ##   bids = orderbook.get("bids", [])
 ##   asks = orderbook.get("asks", [])

    ##print("\n==============================")
    ##print("TOP OF BOOK")
   ## print("==============================")

  ##  if bids:
  ##      print(f"Best bid: {bids[0]}")
  ##  else:
  ##      print("No bids returned")

 ##   if asks:
 ##       print(f"Best ask: {asks[0]}")
 ##   else:
 ##       print("No asks returned")


if __name__ == "__main__":
    main()