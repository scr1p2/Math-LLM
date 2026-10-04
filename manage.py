#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import logging
import os
import sys
import time

OLLAMA_RETRY_INTERVAL_SECONDS = 5
OLLAMA_REQUEST_TIMEOUT_SECONDS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _wait_for_ollama():
    """Wait until the local Ollama service responds to a model-list request."""

    from ollama import Client

    client = Client(timeout=OLLAMA_REQUEST_TIMEOUT_SECONDS)
    attempt = 0
    logger.info("Waiting for Ollama to become available before starting Django.")

    while True:
        attempt += 1
        try:
            client.list()
        except Exception as exc:
            logger.warning(
                "Ollama is not ready (attempt %d): %s. Retrying in %d seconds.",
                attempt,
                exc,
                OLLAMA_RETRY_INTERVAL_SECONDS,
            )
            time.sleep(OLLAMA_RETRY_INTERVAL_SECONDS)
        else:
            logger.info(
                "Ollama is running and responding; starting Django "
                "after %d attempt(s).",
                attempt,
            )
            return


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mathllm.settings')
    if len(sys.argv) > 1 and sys.argv[1] == "runserver":
        _wait_for_ollama()
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
