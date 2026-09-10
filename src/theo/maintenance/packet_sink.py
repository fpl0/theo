"""Discard a builder VM's Ethernet frames without opening a host network interface.

The pinned Tart driver connects an inherited Unix datagram socket as its virtual
network backend. This helper only reads and discards packets: it cannot route,
resolve names, reply, or connect to a host service. It needs no elevated identity.
Install its isolated-Python wrapper as `softnet` in the driver's private PATH;
never substitute the routable Softnet executable when launching a builder.
"""

import argparse
import os
import re
import signal
import socket


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--vm-fd", type=int, required=True, choices=(0,))
    parser.add_argument("--vm-mac-address", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", args.vm_mac_address):
        parser.error("Expected a virtual Ethernet MAC address")
    parent = os.getppid()
    if parent <= 1:
        parser.error("The packet sink requires a live VM driver parent")
    channel = socket.socket(fileno=args.vm_fd)
    if channel.family != socket.AF_UNIX or channel.type != socket.SOCK_DGRAM:
        channel.close()
        parser.error("Expected the driver's inherited Unix datagram socket")
    stopped = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    channel.settimeout(0.25)
    print("theo-disconnected-network-ready", flush=True)
    try:
        while not stopped and os.getppid() == parent:
            try:
                channel.recv(65536)
            except TimeoutError:
                continue
    finally:
        channel.close()


if __name__ == "__main__":
    main()
