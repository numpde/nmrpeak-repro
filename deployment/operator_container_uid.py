"""Map the invoking host user into the selected Docker engine."""

import os
from pathlib import Path
import sys

from deployment.provider_deployment import DeploymentOperationRejected, _container_uid_map


def container_uid(host_uid, mappings):
    try:
        return int(mappings.translate_inverse(host_uid))
    except ValueError as error:
        raise DeploymentOperationRejected("Selected Docker engine did not map the operator UID exactly once") from error


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Supply the pinned target image for the selected Docker engine.")
    mappings = _container_uid_map(Path("/usr/bin/docker"), sys.argv[1])
    print(container_uid(os.geteuid(), mappings))


if __name__ == "__main__":
    main()
