#!/usr/bin/env python3
"""Find Roku devices on the local network and optionally save one to config.json.

Usage:
  python3 discover.py          # list Rokus found on the LAN
  python3 discover.py --save   # also write the first one found to config.json

Tries SSDP multicast first (instant), then falls back to scanning the local
/24 subnet for ECP responders on port 8060.
"""
import argparse
import json
import re
import socket
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


def ssdp_search(timeout=3):
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nST: roku:ecp\r\nMX: 2\r\n\r\n').encode()
    found = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, ("239.255.255.250", 1900))
        while True:
            data, addr = sock.recvfrom(1024)
            match = re.search(r"http://([\d.]+):8060", data.decode(errors="replace"))
            found.add(match.group(1) if match else addr[0])
    except OSError:
        pass
    finally:
        sock.close()
    return sorted(found)


def local_subnet():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    finally:
        sock.close()
    return ip.rsplit(".", 1)[0]


def subnet_scan():
    prefix = local_subnet()
    print(f"SSDP found nothing; scanning {prefix}.0/24 for port 8060...")

    def probe(ip):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            return ip if sock.connect_ex((ip, 8060)) == 0 else None
        finally:
            sock.close()

    with ThreadPoolExecutor(max_workers=64) as pool:
        hits = pool.map(probe, (f"{prefix}.{i}" for i in range(1, 255)))
    return [ip for ip in hits if ip]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", action="store_true",
                        help="write the first Roku found to config.json")
    args = parser.parse_args()

    candidates = ssdp_search() or subnet_scan()
    rokus = [r for r in map(describe, candidates) if r]
    if not rokus:
        print("No Roku devices found. Is the TV plugged in and on this network?")
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
