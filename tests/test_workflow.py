import os, yaml

WF = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".github", "workflows", "scan.yml")
wf = yaml.safe_load(open(WF))
on = wf.get("on", wf.get(True))


def test_yaml_valid_and_schedule_every_15_min():
    assert on["schedule"][0]["cron"] == "*/15 * * * *"
    assert "workflow_dispatch" in on


def test_concurrency_protection_and_timeout():
    assert wf["concurrency"]["group"] and wf["concurrency"]["cancel-in-progress"] is False
    assert wf["jobs"]["scan"]["timeout-minutes"] <= 15


def test_state_branch_persistence_and_always_save():
    steps = wf["jobs"]["scan"]["steps"]
    restore = [s for s in steps if s.get("id") == "restore"][0]
    assert "state_sync.py restore" in restore["run"]
    save = [s for s in steps if s.get("name", "").startswith("Save")][0]
    assert save["if"].startswith("always()") and "restore.outcome" in save["if"] and "state_sync.py push" in save["run"]
    assert wf["permissions"]["contents"] == "write"
    assert wf["jobs"]["scan"]["env"]["STATE_SYNC_CHECKPOINT"] == "1"


def test_four_keys_plus_optional_gold_and_oanda_wired_no_three_key_leftovers():
    env = wf["jobs"]["scan"]["env"]
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TWELVE_DATA_API_KEY_1", "TWELVE_DATA_API_KEY_2",
              "TWELVE_DATA_API_KEY_3", "TWELVE_DATA_API_KEY_4", "TWELVE_DATA_API_KEY_GOLD", "OANDA_API_TOKEN"):
        assert "secrets." + k in env[k]
