import asyncio

import pytest

from forge.errors import InvalidInput
from tests.test_browser import site


async def test_browser_new_tabs_and_login_forms(services, site):
    from forge.browser.agent import BrowserManager
    manager = BrowserManager(services.cfg.home, headless=True, action_delay_ms=0)
    try:
        await manager.open(f"{site}/form", "jobs", allow_hosts=None)
        first = manager._pages["jobs"]
        await manager.open(f"{site}/done", "jobs", allow_hosts=None, new_tab=True)
        second = manager._pages["jobs"]
        assert first is not second and first.url.endswith("/form")
        assert len(first.context.pages) == 2
        for markup, expected_form, expected_required in [
            ("<title>Jobs</title><main>Job details</main><input type=password hidden>", False, False),
            ("<title>Jobs</title><main>Job details</main><input type=password>", True, False),
            ("<title>Sign in</title><input type=password>", True, True),
        ]:
            await second.set_content(markup)
            result = await manager.check_challenge(second)
            assert result["login_form_present"] == expected_form
            assert result["login_required"] == expected_required
    finally:
        await manager.close()


async def test_download_of_web_link_fails_before_long_timeout(services, site, tmp_path):
    from forge.browser.agent import BrowserManager
    manager = BrowserManager(services.cfg.home, headless=True, action_delay_ms=0)
    try:
        await manager.open(f"{site}/form", None, allow_hosts=None)
        snap = await manager.snapshot(None)
        link = next(e for e in snap["elements"] if e["name"] == "Other page")
        with pytest.raises(InvalidInput, match="new_tab=true"):
            await asyncio.wait_for(manager.download(None, link["id"], tmp_path), 5)
    finally:
        await manager.close()
