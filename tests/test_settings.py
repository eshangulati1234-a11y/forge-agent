import pytest

from forge import settings
from forge.config import config_path, reload_config
from forge.errors import InvalidInput, PermissionDenied
from forge.policy.engine import Permissions
from forge.tools.invoke import invoke_tool
from forge.tools.registry import ToolContext


def test_set_round_trip_preserves_comments(home):
    reload_config()
    before = config_path().read_text(encoding="utf-8")
    assert "# never expose" in before
    r = settings.set_value("daemon.max_concurrent_runs", "4")
    assert r == {"key": "daemon.max_concurrent_runs", "old": 2, "new": 4, "apply": "live"}
    after = config_path().read_text(encoding="utf-8")
    assert "# never expose" in after and "# Add the RTX 3060 desktop later" in after
    assert reload_config().daemon.max_concurrent_runs == 4
    assert list((home / "backups").glob("forge.toml.*"))


def test_invalid_values_rejected_and_file_untouched(home):
    reload_config()
    before = config_path().read_text(encoding="utf-8")
    with pytest.raises(InvalidInput):
        settings.set_value("daemon.port", "not-a-number")
    with pytest.raises(InvalidInput, match="unknown setting"):
        settings.set_value("daemon.nope", "1")
    with pytest.raises(InvalidInput):
        settings.set_value("models", '[{"id": "x"}]')          # missing required node/capabilities
    assert config_path().read_text(encoding="utf-8") == before


def test_dangerous_host_needs_confirmation(home):
    reload_config()
    with pytest.raises(InvalidInput, match="exposes"):
        settings.set_value("daemon.host", "0.0.0.0")
    assert settings.set_value("daemon.host", "0.0.0.0", confirm_danger=True)["apply"] == "restart"


def test_restart_keys_flagged(home):
    reload_config()
    assert settings.find(reload_config(), "nodes").apply == "restart"
    assert settings.find(reload_config(), "laya.accept_threshold").apply == "live"


async def test_live_apply_pins_thresholds_and_disabled_tools(services):
    settings.set_value("routing.pins", '{"reasoning": "qwen3:4b"}')
    settings.set_value("laya.accept_threshold", "0.42")
    settings.set_value("tools.disabled", "test.*")
    settings.apply_live(services, reload_config())
    assert services.router.pinned == {"reasoning": "qwen3:4b"}
    assert services.cfg.laya.accept_threshold == 0.42
    assert services.router.candidates("reasoning")[0].id == "qwen3:4b"
    with pytest.raises(PermissionDenied, match="disabled"):
        await invoke_tool(services, "test.echo", {"value": 1}, Permissions(tools=["*"]),
                          ToolContext(services, run_id="r", workflow_id="w", step_path="s"), interactive=False)
    assert "test.echo" not in {t.name for t in services.registry.search("echo")}
    services.registry.disabled = []
