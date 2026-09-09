from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        print("title=", page.title())
        print("url=", page.url)
        print("library_buttons=", page.locator("#library-btn").count())
        if page.locator("#library-btn").count():
            button = page.locator("#library-btn")
            print("library_visible=", button.is_visible(), "enabled=", button.is_enabled())
        print("library_dialog_hidden=", page.locator("#library-dialog").evaluate("el => el.classList.contains('hidden')"))
        print("button_texts=", page.locator("button").all_text_contents()[:30])
        screenshot = Path("work/ui-capture/library-inspect.png")
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot), full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
