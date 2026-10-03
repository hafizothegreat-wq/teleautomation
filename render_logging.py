"""Render-friendly logging for the Telegram automation service.

Render (https://render.com) captures everything a service writes to stdout /
stderr and streams it to the service's *Logs* tab. Raw ``print()`` calls work,
but they carry no level, timestamp or thread information, which makes a
threaded service like this one hard to read once it is deployed. This module
wires the standard-library :mod:`logging` module up so that every event is a
single, greppable, timestamped line::

    2026-10-03T12:00:00Z INFO    [QueueProcessor] telegramheadless: Queue processor started

Design notes
------------
* **stdout** is used on purpose -- Render reads stdout/stderr, and logging to
  stdout also plays nicely with ``PYTHONUNBUFFERED=1`` (see the Dockerfile).
* **UTC, ISO-8601** timestamps so log lines are stable regardless of the
  container's timezone.
* The **thread name** is included because the HTTP server, the session cleaner
  and the queue processor all run as separate threads.
* **Colours** are only emitted for an interactive TTY (local dev); Render's log
  stream has no TTY, so deployed logs stay plain text and greppable.
* Third-party loggers (``urllib3``/``selenium``/``undetected_chromedriver``/
  ``google``) are tamed so they do not drown out the application logs.

Usage
-----
    from render_logging import setup_logging, get_logger, log_startup_banner

    log = setup_logging()          # once, at import/startup
    log.info("hello")
    log_startup_banner(log)        # optional: dump deploy/env context

Environment
-----------
    LOG_LEVEL   DEBUG | INFO | WARNING | ERROR | CRITICAL   (default ``INFO``)
    LOG_FORMAT  optional ``logging.Formatter`` format string to override the line
    NO_COLOR    set to any value to disable ANSI colours even on a TTY
    FORCE_COLOR set to any value to force ANSI colours even without a TTY
"""
from __future__ import annotations

import logging
import os
import sys
import time

DEFAULT_LOGGER_NAME = "telegramheadless"

#: Render injects these into the service environment. We log them at startup so
#: it is obvious which deploy/instance a given log stream belongs to.
RENDER_ENV_KEYS = (
    "RENDER",
    "RENDER_SERVICE_ID",
    "RENDER_SERVICE_NAME",
    "RENDER_SERVICE_TYPE",
    "RENDER_EXTERNAL_URL",
    "RENDER_GIT_COMMIT",
    "RENDER_GIT_BRANCH",
    "RENDER_INSTANCE_ID",
)

#: Non-secret knobs that shape the bot's behaviour and are handy in the banner.
STARTUP_ENV_KEYS = (
    "PORT",
    "HEADLESS",
    "DISPLAY",
    "CHROME_PATH",
    "CHROME_INSTALL_DIR",
    "AUTO_INSTALL_CHROME",
    "INSTALL_CHROME_LIBS",
    "WARMUP_BROWSER",
    "LOG_LEVEL",
    "LOGIN_TIMEOUT",
    "CODE_WAIT",
    "CODE_ENTRY_TIMEOUT",
    "SESSION_TTL",
    "QUEUED_TTL",
)

DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"

_RESET = "\033[0m"
_LEVEL_COLORS = {
    "DEBUG": "\033[36m",       # cyan
    "INFO": "\033[32m",        # green
    "WARNING": "\033[33m",     # yellow
    "ERROR": "\033[31m",       # red
    "CRITICAL": "\033[1;31m",  # bold red
}

#: Libraries whose default levels are too chatty for a production log stream.
_NOISY_LOGGERS = (
    "urllib3",
    "selenium",
    "undetected_chromedriver",
    "websockets",
    "chardet",
    "asyncio",
    "google",
    "google.auth",
    "google.api_core",
    "google.cloud",
)

_configured = False


class _RenderFormatter(logging.Formatter):
    """UTC ISO-8601 timestamps with optional ANSI colours on the level name."""

    converter = time.gmtime

    def __init__(self, *args, color: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self._color:
            color = _LEVEL_COLORS.get(record.levelname)
            if color:
                # Only the first occurrence (the level column) is colourised.
                text = text.replace(record.levelname, f"{color}{record.levelname}{_RESET}", 1)
        return text


def is_render() -> bool:
    """Return True when running inside a Render service instance."""
    return any(
        os.environ.get(key)
        for key in ("RENDER", "RENDER_SERVICE_ID", "RENDER_INSTANCE_ID")
    )


def render_env() -> dict:
    """Return the Render-provided environment variables that are actually set."""
    return {key: os.environ[key] for key in RENDER_ENV_KEYS if os.environ.get(key)}


def resolve_level(default: int = logging.INFO) -> int:
    """Resolve the effective log level from the ``LOG_LEVEL`` env var."""
    raw = os.environ.get("LOG_LEVEL", "").strip().upper()
    if not raw:
        return default
    level = logging.getLevelName(raw)
    return level if isinstance(level, int) else default


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def setup_logging(name: str = DEFAULT_LOGGER_NAME, level: int | None = None, stream=None, force: bool = False):
    """Configure root logging for Render. Idempotent; returns a ``Logger``.

    The handler/formatter are installed once; the level is (re)applied on every
    call so callers may raise or lower verbosity at runtime. Pass ``force=True``
    to reinstall the handler (e.g. to redirect the log stream).
    """
    global _configured

    stream = stream if stream is not None else sys.stdout
    level = resolve_level() if level is None else level

    if force or not _configured:
        fmt = os.environ.get("LOG_FORMAT") or DEFAULT_FORMAT
        formatter = _RenderFormatter(
            fmt, datefmt=DEFAULT_DATEFMT, color=_supports_color(stream)
        )
        handler = logging.StreamHandler(stream)
        handler.setFormatter(formatter)

        root = logging.getLogger()
        # Remove handlers installed by libraries so we own the output format.
        for existing in list(root.handlers):
            root.removeHandler(existing)
        root.addHandler(handler)

        for noisy in _NOISY_LOGGERS:
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _configured = True

    logging.getLogger().setLevel(level)
    logger = logging.getLogger(name)
    logger.debug(
        "Logging configured (level=%s, stream=%s)",
        logging.getLevelName(level),
        getattr(stream, "name", repr(stream)),
    )
    return logger


def get_logger(name: str = DEFAULT_LOGGER_NAME) -> logging.Logger:
    """Return the named logger (configuration is done by :func:`setup_logging`)."""
    return logging.getLogger(name)


def log_startup_banner(logger: logging.Logger | None = None) -> logging.Logger:
    """Log the deploy/environment context once at startup.

    Deliberately excludes secrets (``FIREBASE_KEY``, ``ADMIN_PASS``) -- only
    non-sensitive platform/behaviour knobs are printed.
    """
    logger = logger or get_logger()
    platform = "Render" if is_render() else "local/unknown"
    logger.info("=" * 64)
    logger.info("Telegram automation starting (platform=%s)", platform)
    for key, value in render_env().items():
        logger.info("render %s = %s", key, value)
    for key in STARTUP_ENV_KEYS:
        value = os.environ.get(key)
        if value not in (None, ""):
            logger.info("env %s = %s", key, value)
    logger.info("=" * 64)
    return logger