"""
Log filter that scrubs credentials out of log records: the Plex token
(``X-Plex-Token``), the plex.tv login token (``authToken``), the JWT
(``access_token`` / ``?token=``), and Fernet ciphertext blobs.

python-plexapi commonly raises exceptions whose ``__str__`` contains
the full request URL, and Plex's URLs carry the auth token as a
query-string parameter. If those exceptions are logged verbatim (or
captured by ``traceback.print_exc``), the token lands on disk in
``runtime.log``, ``errors.log``, the Docker container's stdout, and
anywhere else the configured handlers route to.

This filter is installed on every logging handler - engine,
``plexmigrate.server.*``, ``uvicorn``, ``uvicorn.error``,
``uvicorn.access``, and the root logger - so no matter which code
path emits the credential-bearing message, the final on-disk /
on-screen form has ``<redacted>`` in place of the real value.

It does NOT cover writes that bypass the logging framework. The job
worker's ``traceback.print_exc()`` (which went to stderr direct) has
been replaced with ``log.error(..., exc_info=True)`` so the filter
catches that path too.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Iterable

# M7: catches the credential-bearing ``key=value`` / ``key: value``
# forms for every secret that turns up in our logs - the Plex token
# (``X-Plex-Token``), the plex.tv login token (``authToken``), the
# JWT (``access_token`` and the bare ``?token=`` WebSocket form), and
# any ``password`` field (covers ``db_admin_password`` and friends if
# a request body ever reaches a log line). Longer keys are listed
# before the bare ``token`` alternative so the alternation matches
# them whole. The separator clause ``["']?\s*[=:]\s*["']?`` absorbs
# the quoting in JSON / dict-repr forms (``"password": "value"``) as
# well as the bare URL / header forms. The value stops at characters
# that can't appear in any of these: ``&``, ``"``, ``'``, ``<``,
# ``>``, whitespace. The capture group preserves the prefix verbatim
# so we can rebuild the line with the original separator intact.
_TOKEN_PATTERN = re.compile(
    r"((?:X-Plex-Token|authToken|access_token|password|token)[\"']?\s*[=:]\s*[\"']?)"
    r"([^&\s\"'<>]+)",
    re.IGNORECASE,
)

_REDACTED_REPLACEMENT = r"\1<redacted>"

# M7: Fernet ciphertext blobs (encrypted Plex / managed-user tokens at
# rest) are base64url and always start with the version+timestamp
# prefix ``gAAAAA``. They have no ``key=`` lead-in, so they need their
# own pattern. The ``{24,}`` floor is well below a real Fernet token's
# length but high enough to avoid matching incidental text.
_FERNET_PATTERN = re.compile(r"gAAAAA[A-Za-z0-9_\-=]{24,}")

_FERNET_REPLACEMENT = "<redacted-fernet>"


def scrub(text: str) -> str:
    """Strip token ``key=value`` pairs and Fernet blobs from ``text``."""
    text = _TOKEN_PATTERN.sub(_REDACTED_REPLACEMENT, text)
    return _FERNET_PATTERN.sub(_FERNET_REPLACEMENT, text)


class TokenScrubFilter(logging.Filter):
    """
    Strips ``X-Plex-Token=<value>`` from a log record before any
    formatter sees it.

    Filters run before formatters, so we mutate ``record.msg`` /
    ``record.args`` / ``record.exc_text`` here and the downstream
    formatter renders the redacted version. Subsequent filters on
    the same record see the already-redacted state - idempotent.

    Never raises: a filter that throws would silently drop the
    record and the end user would lose the log entry entirely.
    Errors here are swallowed with a best-effort fallback (return
    the record unmodified).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # 1) Merge args into msg, scrub, replace.
            #    ``getMessage`` is the same call formatters make to
            #    produce the final line; doing it here and stashing
            #    the result in ``record.msg`` means later formatters
            #    see the scrubbed text without re-rendering.
            merged = record.getMessage()
            scrubbed = scrub(merged)
            if scrubbed != merged:
                record.msg = scrubbed
                record.args = ()

            # 2) Pre-render exc_info to text and stash on exc_text,
            #    then clear exc_info so formatters don't re-render
            #    the raw (un-scrubbed) traceback.
            if record.exc_info and not record.exc_text:
                exc_lines = traceback.format_exception(*record.exc_info)
                record.exc_text = scrub("".join(exc_lines))
                record.exc_info = None
        except Exception:
            # A filter must not raise - fall through and let the
            # record through unmodified rather than dropping it.
            pass
        return True


# Module-level singleton so the same filter object can be installed
# on many handlers without each holding a distinct instance.
_FILTER = TokenScrubFilter()


def install_on_handler(handler: logging.Handler) -> None:
    """Add the scrubber to ``handler`` unless it already carries one."""
    for existing in handler.filters:
        if isinstance(existing, TokenScrubFilter):
            return
    handler.addFilter(_FILTER)


def _iter_known_loggers() -> Iterable[logging.Logger]:
    """Yield the root logger plus every named logger Python knows about."""
    yield logging.getLogger()
    for name in list(logging.Logger.manager.loggerDict.keys()):
        # ``loggerDict`` can contain ``PlaceHolder`` instances; only
        # real Logger objects have ``.handlers``.
        obj = logging.Logger.manager.loggerDict.get(name)
        if isinstance(obj, logging.Logger):
            yield obj


def install_on_all_handlers() -> None:
    """
    Install the scrubber on every handler currently attached to any
    logger Python knows about.

    Idempotent - re-running adds nothing new. Safe to call multiple
    times (FastAPI startup, ``setup_logging`` per job, etc.) so newly
    created handlers always pick up the filter.
    """
    for logger in _iter_known_loggers():
        for handler in logger.handlers:
            install_on_handler(handler)
