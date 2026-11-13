"""Map the invoking host user into the selected Docker engine."""

import os
import sys

from deployment.engine_identity import project_uids
from repository_checks.checkpoint import docker_host


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Supply the pinned target image for the selected Docker engine.")
    print(project_uids(docker_host(), sys.argv[1], (os.geteuid(),), inverse=True)[0])


if __name__ == "__main__":
    main()
