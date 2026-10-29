"""Inject retirement wire failure into the installed SDK's actual child owner."""

import sys
import time

from provider_core import runner


def main():
    partial = sys.argv[1] == "partial"
    if partial:
        original = runner.send_document

        def send_document(control, document, *, deadline):
            if document.get("kind") == "retired":
                control.sendall(b"\0")
                time.sleep(0.5)
                return
            return original(control, document, deadline=deadline)

        runner.send_document = send_document
    runner.main(sys.argv[2:])


if __name__ == "__main__":
    main()
