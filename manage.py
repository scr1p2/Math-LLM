#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import logging
import os
import sys
import time

OLLAMA_RETRY_INTERVAL_SECONDS = 5
OLLAMA_REQUEST_TIMEOUT_SECONDS = 5

logger = logging.getLogger("manage")


def _wait_for_ollama():
    """Wait until the local Ollama service responds to a model-list request."""

    from ollama import Client

    client = Client(timeout=OLLAMA_REQUEST_TIMEOUT_SECONDS)
    attempt = 0
    started_at = time.monotonic()
    logger.info(
        "startup.ollama.wait.begin timeout_seconds=%s retry_interval_seconds=%s",
        OLLAMA_REQUEST_TIMEOUT_SECONDS,
        OLLAMA_RETRY_INTERVAL_SECONDS,
    )

    while True:
        attempt += 1
        try:
            logger.debug("startup.ollama.check.begin attempt=%s", attempt)
            client.list()
        except Exception as exc:
            logger.warning(
                "startup.ollama.check.failed attempt=%s retry_in_seconds=%s error=%s",
                attempt,
                OLLAMA_RETRY_INTERVAL_SECONDS,
                exc,
            )
            time.sleep(OLLAMA_RETRY_INTERVAL_SECONDS)
        else:
            logger.info(
                "startup.ollama.wait.complete attempts=%s elapsed_seconds=%.2f",
                attempt,
                time.monotonic() - started_at,
            )
            return


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mathllm.settings')
    try:
        import django
        from django.conf import settings
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc

    django.setup()
    if len(sys.argv) > 1 and sys.argv[1] == "runserver" and settings.DEBUG:
        _wait_for_ollama()
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
