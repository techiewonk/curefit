"""Shared rich console + structlog setup for the Curefit scraper."""
import logging

from rich.console import Console
from rich.logging import RichHandler
import structlog

console = Console()

_configured = False


def setup_logging(level=logging.INFO):
    """Configure structlog to render pretty, structured logs through rich."""
    global _configured
    if _configured:
        return structlog.get_logger()

    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=[RichHandler(console=console, rich_tracebacks=True,
                              show_path=False, show_time=False, markup=True)],
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
    )
    for h in logging.getLogger().handlers:
        h.setFormatter(formatter)

    _configured = True
    return structlog.get_logger()


def get_logger():
    return setup_logging()
