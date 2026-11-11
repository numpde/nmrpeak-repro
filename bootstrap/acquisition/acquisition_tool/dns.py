"""Capture configured host resolvers for one interface-bound acquisition scope."""

import argparse
import ipaddress
import os
import socket
import subprocess
from pathlib import Path


class ResolverCaptureFailed(OSError):
    """A safe explanation of why the scope has no usable resolver capture."""


def _addresses(values):
    servers = []
    for value in values:
        try:
            address = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            continue
        if not address.is_loopback and str(address) not in servers:
            servers.append(str(address))
    return tuple(servers)


def _manager_output(command):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}, timeout=3, check=False
        )
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired as cause:
        raise ResolverCaptureFailed(
            "Cannot capture acquisition DNS: the host network manager did not answer within three seconds."
        ) from cause
    # An absent service or unmanaged link supplies no configuration. Consult
    # the other configured source, never an invented public resolver.
    if result.returncode != 0:
        return ""
    return result.stdout


def capture_dns_servers(interface):
    """Prefer link DNS; otherwise retain configured global DNS, still route-bound.

    Capture occurs once before online work. A usable link set is never extended
    with global servers, and query failures never change the captured source.
    Network changes take effect in the next acquisition scope.
    """
    if interface != "system":
        socket.if_nametoindex(interface)
        output = _manager_output(["nmcli", "--get-values", "IP4.DNS", "device", "show", interface])
        servers = _addresses(output.split())
        if servers:
            return servers
        output = _manager_output(["resolvectl", "dns", interface])
        servers = _addresses(output.partition(":")[2].split())
        if servers:
            return servers
    for path in (Path("/run/systemd/resolve/resolv.conf"), Path("/etc/resolv.conf")):
        try:
            lines = path.read_text(encoding="ascii").splitlines()
        except OSError:
            continue
        values = [fields[1] for line in lines if len(fields := line.split()) == 2 and fields[0] == "nameserver"]
        servers = _addresses(values)
        if servers:
            return servers
    raise ResolverCaptureFailed("Cannot capture acquisition DNS: configure an IPv4 resolver for the selected interface or host.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        servers = capture_dns_servers(args.interface)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.writelines(f"nameserver {server}\n" for server in servers)
    except ResolverCaptureFailed as error:
        parser.exit(2, f"{error}\n")
    except (OSError, UnicodeError) as error:
        parser.exit(
            2,
            f"Cannot prepare acquisition resolver capture ({type(error).__name__}); check the selected interface's configured DNS and the private output path.\n",
        )


if __name__ == "__main__":
    main()
