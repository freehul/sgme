#!/usr/bin/env python3
"""批量截取卖点框架图（HTML → PNG，Edge 无头模式）。"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

SRC = Path("assets/src")
OUT = Path("assets")
PAGES = [
    "selling-point-01-trace",
    "selling-point-02-shared-memory",
    "selling-point-03-unified-search",
    "selling-point-04-wiki",
    "selling-point-05-skillhub",
    "selling-point-07-scenario-inject",
    "selling-point-09-selfhosted",
]

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=1)
    for name in PAGES:
        html = SRC / f"{name}.html"
        if not html.exists():
            print("SKIP(缺源):", name)
            continue
        pg.goto(html.resolve().as_uri())
        pg.wait_for_timeout(1200)
        pg.screenshot(path=str(OUT / f"{name}.png"))
        print("OK:", name)
    b.close()
print("DONE")
