import asyncio
from pathlib import Path

import tomlkit


def test_watchdog_runs_inside_task(monkeypatch, tmp_path):
    import thetagang.thetagang as tg

    base_config = tomlkit.parse(
        Path("thetagang.toml").read_text(encoding="utf8")
    ).unwrap()
    if "meta" in base_config and base_config.get("meta", {}).get("schema_version") == 2:
        base_config["runtime"]["database"]["enabled"] = False
        base_config["runtime"]["ib_async"]["logfile"] = ""
        stages = base_config.get("run", {}).get("stages", [])
        if isinstance(stages, list):
            base_config["run"].pop("strategies", None)
            base_config["run"]["stages"] = [
                {
                    "id": "options_write_puts",
                    "kind": "options.write_puts",
                    "enabled": True,
                }
            ]
    else:
        base_config["database"]["enabled"] = False
        base_config["ib_async"]["logfile"] = ""
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text(tomlkit.dumps(tomlkit.item(base_config)), encoding="utf8")

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(tg.util, "getLoop", lambda: loop)
    monkeypatch.setattr(tg, "need_to_exit", lambda *_: False)

    captured = {}

    class DummyEvent:
        def __init__(self):
            self._handlers = []

        def __iadd__(self, handler):
            self._handlers.append(handler)
            return self

        def __isub__(self, handler):
            self._handlers.remove(handler)
            return self

    class FakeContract:
        def __init__(self, **_kwargs):
            pass

    class FakeIBC:
        def __init__(self, tws_version, **_kwargs):
            self.twsVersion = tws_version
            self.terminated = False
            captured["ibc"] = self

        async def terminateAsync(self):
            self.terminated = True

    class FakeWatchdog:
        def __init__(self, *_args, **_kwargs):
            self.started = False
            self.stopped = False
            captured["watchdog"] = self

        def start(self):
            assert asyncio.get_running_loop() is loop
            self.started = True

        def stop(self):
            self.stopped = True

    class FakeIB:
        def __init__(self):
            self.connectedEvent = DummyEvent()
            self.RaiseRequestErrors = False

        def run(self, awaitable):
            assert asyncio.iscoroutine(awaitable)
            loop.run_until_complete(awaitable)
            loop.stop()
            loop.close()

    class FakePortfolioManager:
        def __init__(
            self,
            _config,
            _ib,
            completion_future,
            _dry_run,
            data_store=None,
            run_stage_flags=None,
            run_stage_order=None,
        ):
            if not completion_future.done():
                completion_future.set_result(True)

    monkeypatch.setattr(tg, "IBC", FakeIBC)
    monkeypatch.setattr(tg, "Watchdog", FakeWatchdog)
    monkeypatch.setattr(tg, "IB", FakeIB)
    monkeypatch.setattr(tg, "PortfolioManager", FakePortfolioManager)
    monkeypatch.setattr(tg, "Contract", FakeContract)

    tg.start(
        str(config_path),
        without_ibc=False,
        dry_run=True,
        auto_approve_migration=False,
    )

    assert captured["watchdog"].started is True
    assert captured["watchdog"].stopped is True
    assert captured["ibc"].terminated is True
