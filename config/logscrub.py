"""Logging filter: the signed Metabase embed token is a credential, so it never reaches the console or the log file."""
import logging


class ScrubToken(logging.Filter):
    """Applies kronos.scrub to the message and to the traceback text of every record a handler writes."""

    def filter(self, record):
        from scraper.kronos import scrub
        try:
            record.msg, record.args = scrub(record.getMessage()), ()
        except Exception:  # a broken format string must not lose the record
            record.msg, record.args = scrub(record.msg), ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        return True
