#!/usr/bin/env python3
"""
Scan the local network for pyatv-compatible devices (HomePods, Apple TVs).
Run this to find identifiers to add to ~/.spotify_display.conf

Usage:
    python3 /home/kadn/scan_airplay.py
"""
import asyncio, sys

try:
    import pyatv
except ImportError:
    print("pyatv not installed. Run: pip3 install pyatv")
    sys.exit(1)

async def main():
    print("Scanning network for AirPlay/Apple devices (10s)...\n")
    loop = asyncio.get_event_loop()
    devices = await pyatv.scan(loop, timeout=10)

    if not devices:
        print("No devices found. Make sure you're on the same network.")
        return

    homepods = [d for d in devices if "HomePod" in (d.device_info.model or "")]
    appletvs = [d for d in devices if "HomePod" not in (d.device_info.model or "")]

    if homepods:
        print(f"HomePods ({len(homepods)} found):")
        for d in homepods:
            print(f"  {d.name:<30} id: {d.identifier}")

    if appletvs:
        print(f"\nApple TVs / other ({len(appletvs)} found):")
        for d in appletvs:
            model = d.device_info.model or "Unknown"
            print(f"  {d.name:<30} id: {d.identifier}  ({model})")

    print("\nTo add an Apple TV, copy its id into ~/.spotify_display.conf:")
    print('  "appletv_ids": ["PASTE-ID-HERE"]')

asyncio.run(main())
