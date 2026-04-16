import logging

from app.daily_briefing import run_daily_briefing

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level="INFO",
)
logger = logging.getLogger("openbrain.daily_briefing_runner")


def main() -> None:
    result = run_daily_briefing()
    if result:
        logger.info("Daily briefing created for %s", result.date_label)
    else:
        logger.info("Daily briefing skipped")


if __name__ == "__main__":
    main()
