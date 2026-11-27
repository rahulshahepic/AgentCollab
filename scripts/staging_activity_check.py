"""Verify keyboard access to a known synthetic fixture thread at phone width."""

import argparse
import os

from scripts.staging_smoke import FIXTURE_ORIGIN, RUN_ID, SmokeFailure, names, require


def verify_keyboard(page, run_id):
    request = f"Synthetic fixture request {run_id}"
    details = page.locator("details.transaction").filter(has_text=request)
    summary = details.locator("summary")
    summary.wait_for()
    require(page.evaluate("innerWidth === 390 && document.documentElement.scrollWidth === innerWidth"),
            "Activity fits a 390 CSS pixel viewport")
    for _ in range(60):
        page.keyboard.press("Tab")
        if summary.evaluate("(element) => element === document.activeElement"):
            break
    else:
        raise SmokeFailure("Tab did not reach the synthetic thread")
    page.keyboard.press("Enter")
    require(details.evaluate("(element) => element.open"), "Enter expands the thread")
    sender, receiver = names(run_id)
    require(f"{sender} → {receiver}" in summary.inner_text()
            and details.get_by_text(f"Reply from {receiver}", exact=True).is_visible()
            and details.get_by_text(f"Synthetic fixture reply {run_id}", exact=True).is_visible(),
            "Expanded Activity shows both agents and the reply")
    page.keyboard.press("Space")
    require(not details.evaluate("(element) => element.open"), "Space collapses the thread")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    args = parser.parse_args()
    try:
        require(RUN_ID.fullmatch(args.run_id) is not None, "Use a synthetic fixture run ID")
        require(os.environ.get("AGENTCOLLAB_FIXTURE_ORIGIN") == FIXTURE_ORIGIN, "Use the isolated fixture origin")
        username = os.environ.get("AGENTCOLLAB_FIXTURE_TEST_USERNAME")
        password = os.environ.get("AGENTCOLLAB_FIXTURE_TEST_PASSWORD")
        require(username and password, "Supply the disposable fixture account via environment variables")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("AGENTCOLLAB_FIXTURE_CHROMIUM") or None,
            )
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                page.set_default_timeout(8000)
                page.goto(f"{FIXTURE_ORIGIN}/login", wait_until="domcontentloaded")
                require(page.url == f"{FIXTURE_ORIGIN}/login", "Fixture sign-in page")
                page.get_by_label("Username").fill(username)
                page.get_by_label("Password").fill(password)
                page.get_by_role("button", name="Sign in").click()
                page.wait_for_url(f"{FIXTURE_ORIGIN}/")
                verify_keyboard(page, args.run_id)
            finally:
                browser.close()
    except SmokeFailure as error:
        parser.exit(1, f"Activity check failed: {error}\n")
    except Exception as error:
        parser.exit(1, f"Activity check failed: {type(error).__name__}\n")
    print(f"Fixture Chromium keyboard check passed at 390 CSS px for run {args.run_id}.")


if __name__ == "__main__":
    main()
