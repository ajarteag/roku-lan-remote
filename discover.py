#!/usr/bin/env python3
"""Find Roku devices on the local network and optionally save one to config.json.

Usage:
  python3 discover.py                    # list Rokus found on the LAN
  python3 discover.py --save             # also write the first one to config.json
  python3 discover.py --subnet 192.168.4 # force a specific /24 to scan

Tries SSDP multicast on every network interface first (instant), then falls
back to scanning each interface's /24 subnet for ECP responders on port 8060.
Handles multi-homed hosts (e.g. Ethernet on one router + Wi-Fi on another).
"""
import argparse
import json
import re
import socket
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def describe(ip):
    try:
        with urllib.request.urlopen(f"http://{ip}:8060/query/device-info", timeout=2) as resp:
            info = ET.fromstring(resp.read())
        name = info.findtext("friendly-device-name") or info.findtext("friendly-model-name")
        return ip, name
    except OSError:
        return None


def local_ipv4_addresses():
    """All usable local IPv4 addresses, across every interface."""
    for cmd in (["ifconfig"], ["ip", "-4", "addr"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        addrs = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        addrs = [a for a in addrs if not a.startswith(("127.", "169.254."))]
        if addrs:
            return sorted(set(addrs))
    # Fallback: whichever interface routes to the internet.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return [sock.getsockname()[0]]
    except OSError:
        return []
    finally:
        sock.close()


def ssdp_search(source_ip, timeout=3):
    """SSDP M-SEARCH for roku:ecp, sent from a specific local interface."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nST: roku:ecp\r\nMX: 2\r\n\r\n').encode()
    found = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    try:
        sock.bind((source_ip, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(source_ip))
        sock.sendto(msg, ("239.255.255.250", 1900))
        while True:
            data, addr = sock.recvfrom(1024)
            match = re.search(r"http://([\d.]+):8060", data.decode(errors="replace"))
            found.add(match.group(1) if match else addr[0])
    except OSError:
        pass
    finally:
        sock.close()
    return found


def subnet_scan(prefix):
    """TCP-probe port 8060 across a /24."""
    print(f"Scanning {prefix}.0/24 for port 8060...")

    def probe(ip):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            return ip if sock.connect_ex((ip, 8060)) == 0 else None
        finally:
            sock.close()

    with ThreadPoolExecutor(max_workers=64) as pool:
        hits = pool.map(probe, (f"{prefix}.{i}" for i in range(1, 255)))
    return {ip for ip in hits if ip}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", action="store_true",
                        help="write the first Roku found to config.json")
    parser.add_argument("--subnet", action="append", default=[],
                        help="extra /24 to scan, e.g. 192.168.4 (repeatable)")
    args = parser.parse_args()

    local_ips = local_ipv4_addresses()
    if local_ips:
        print("Local interfaces: " + ", ".join(local_ips))

    candidates = set()
    for ip in local_ips:
        candidates |= ssdp_search(ip)

    if not candidates:
        print("SSDP found nothing; falling back to subnet scans.")
        prefixes = {ip.rsplit(".", 1)[0] for ip in local_ips}
        for spec in args.subnet:
            octets = spec.split("/")[0].rstrip(".").split(".")
            prefixes.add(".".join(octets[:3]))
        for prefix in sorted(prefixes):
            candidates |= subnet_scan(prefix)

    rokus = [r for r in map(describe, sorted(candidates)) if r]
    if not rokus:
        print("No Roku devices found. Is the TV plugged in, and is this computer\n"
              "on (or bridged to) the TV's network? Try --subnet <prefix>, e.g.\n"
              "  python3 discover.py --subnet 192.168.4 --save")
        return

    for ip, name in rokus:
        print(f"  {ip}  {name}")

    if args.save:
        config = {}
        if CONFIG_PATH.exists():
            config = json.loads(CONFIG_PATH.read_text())
        config["tv_ip"] = rokus[0][0]
        config.setdefault("server_port", 8000)
        CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
        print(f"\nSaved {rokus[0][0]} to {CONFIG_PATH.name}")


if __name__ == "__main__":
    main()
