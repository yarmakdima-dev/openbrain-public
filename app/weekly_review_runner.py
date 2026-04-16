import logging

from app.job_runs import track_job
from app.weekly_review import run_weekly_review_with_retry

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level="INFO",
)
logger = logging.getLogger("openbrain.weekly_review_runner")


def main() -> None:
    with track_job("weekly_review"):
        result = run_weekly_review_with_retry()
        if result:
            logger.info("Weekly review created for %s", result.date_range_label)
        else:
            logger.info("Weekly review skipped or failed")


if __name__ == "__main__":
    main()
