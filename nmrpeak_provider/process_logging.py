"""Configure stderr logging at provider and runner process entry points."""

import logging
import time


def configure_process_logging() -> None:
    """Replace root handlers with UTC stderr output at process startup.

    Application namespaces emit INFO; dependencies default to WARNING.
    Call only from executable entry points, which own the process-wide sink.
    """

    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s %(threadName)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    logging.getLogger("nmrpeak_provider").setLevel(logging.INFO)
    logging.getLogger("nmrpeak_runner").setLevel(logging.INFO)
