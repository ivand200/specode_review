import argparse


def campaign_main() -> None:
    parser = argparse.ArgumentParser(
        prog="specode-review-real-e2e",
        description="Run the SpeCodeReview signed production-path release campaign.",
    )
    parser.parse_args()
    parser.error("the signed production-path campaign is not implemented yet")
