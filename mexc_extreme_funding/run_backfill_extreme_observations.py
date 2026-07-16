from mexc_extreme_funding.scanner import backfill_extreme_observations


def main() -> None:
    result = backfill_extreme_observations()
    print(
        "MEXC extreme-observation backfill "
        f"files_created={result['files_created']} rows_written={result['rows_written']}"
    )


if __name__ == "__main__":
    main()
