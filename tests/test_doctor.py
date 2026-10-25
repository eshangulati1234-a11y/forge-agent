from forge.config import ForgeConfig, reload_config
from forge.setup.doctor import check_bind, run_checks
from forge.setup.profiles import PROFILES, recommend


def test_all_checks_well_formed(home):
    checks = run_checks(reload_config(), skip={"daemon"})
    assert len(checks) >= 15
    for c in checks:
        assert c.status in ("ok", "warn", "fail")
        assert c.name and isinstance(c.detail, str)
        if c.status != "ok":
            assert c.fix, f"{c.name} has no fix hint"


def test_exposed_api_fails_with_fix():
    cfg = ForgeConfig()
    cfg.daemon.host = "0.0.0.0"
    c = check_bind(cfg)
    assert c.status == "fail" and "127.0.0.1" in c.fix


def test_profile_recommendation():
    assert recommend(8192).name == "gpu-8gb"
    assert recommend(12288).name == "gpu-12gb"
    assert recommend(0).name == "cpu-only"
    assert PROFILES["gpu-12gb-qwen35"].status == "experimental"
