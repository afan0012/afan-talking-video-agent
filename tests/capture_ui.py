from pathlib import Path

from playwright.sync_api import sync_playwright

OUTPUT = Path("work/ui-capture/workbench.png")
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 980}, device_scale_factor=1)
    page.goto("http://127.0.0.1:8000", wait_until="networkidle")
    page.screenshot(path=str(OUTPUT))
    browser.close()
print(OUTPUT)
