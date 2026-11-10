#!/usr/bin/env python3
"""Lokalna weryfikacja: headless Google Chrome (ma H.264) otwiera każdy test i czyta window.__stats.

Użycie: (serwer musi działać)  .venv/bin/python test_local.py [base_url] [sekundy]
"""
import asyncio
import json
import sys

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090"
SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 10

TESTS = [("mse", "?test=mse"), ("mse+audio", "?test=mse&audio=1"), ("mse+opus", "?test=mse&audio=1&acodec=opus"),
         ("webcodecs", "?test=webcodecs"), ("webrtc", "?test=webrtc")]


async def main():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True, args=[
                "--autoplay-policy=no-user-gesture-required"])
        except Exception as e:  # noqa: BLE001
            print("chrome channel unavailable, fallback chromium:", e)
            browser = await p.chromium.launch(headless=True, args=[
                "--autoplay-policy=no-user-gesture-required"])
        results = {}
        for name, qs in TESTS:
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            logs = []
            page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
            page.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
            await page.goto(BASE + "/" + qs)
            await page.wait_for_timeout(SECS * 1000)
            stats = await page.evaluate("window.__stats || null")
            caps = await page.evaluate("window.__caps || null")
            results[name] = stats
            print(f"\n=== {name} ===")
            print(json.dumps(stats, indent=1, ensure_ascii=False))
            if logs:
                print("console:", *logs[:15], sep="\n  ")
            if name == "mse" and caps:
                print("caps (headless Chrome): VideoDecoder=", caps["WebCodecs"]["VideoDecoder"],
                      "barcodeSelfTest=", caps["barcodeSelfTest"],
                      "mse avc1.640028=", caps["mse"].get('video/mp4; codecs="avc1.640028"'))
            await page.close()
            await asyncio.sleep(1.5)
        await browser.close()


asyncio.run(main())
