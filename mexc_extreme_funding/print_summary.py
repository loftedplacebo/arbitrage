from mexc_extreme_funding.config import DEFAULT_CONFIG
from mexc_extreme_funding.dashboard import _summary_payload


def main() -> None:
    payload = _summary_payload(DEFAULT_CONFIG)
    print("MEXC extreme-funding summary")
    for row in payload["items"]:
        print(f"{row['label']}: {row['value']}")


if __name__ == "__main__":
    main()
