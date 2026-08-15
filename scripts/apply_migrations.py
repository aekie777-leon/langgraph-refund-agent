"""Apply all pending PostgreSQL migrations from the command line."""

import logging

from dotenv import load_dotenv

from agent.migrations import apply_migrations

LOGGER = logging.getLogger(__name__)


def main() -> None:
    """Load local environment settings and apply all pending migrations."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()
    applied = apply_migrations()
    if applied:
        LOGGER.info("Applied database migrations: %s", ", ".join(applied))
    else:
        LOGGER.info("Database schema is already up to date.")


if __name__ == "__main__":
    main()
