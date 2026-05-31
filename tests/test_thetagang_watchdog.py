import asyncio
from pathlib import Path

import tomlkit


class DummyEvent:
    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        self._handlers.remove(handler)
        return self

    def emit(self, *args):
        for handler in list(self._handlers):
            handler(*args)


def _write_config(tmp_path, *, max_startup_retries=None):
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
        if max_startup_retries is not None:
            base_config["runtime"]["watchdog"]["maxStartupRetries"] = (
                max_startup_retries
            )
    else:
        base_config["database"]["enabled"] = False
        base_config["ib_async"]["logfile"] = ""
        if max_startup_retries is not None:
            base_config["watchdog"]["maxStartupRetries"] = max_startup_retries
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text(tomlkit.dumps(tomlkit.item(base_config)), encoding="utf8")
    return config_path


def _run_start(monkeypatch, tmp_path, *, max_startup_retries=None, event_script=()):
    """Run ``thetagang.start`` with a fake IB stack.

    ``event_script`` is a sequence of ``"started"``/``"stopped"`` strings that
    the fake watchdog emits (in order) from ``start()`` to simulate connect
    cycles. After replaying the script the fake resolves the completion future
    so the run loop terminates, unless a startup-retry failure has already
    failed the future.

    Returns ``(captured, error)`` where ``error`` is any exception raised out of
    ``start`` (e.g. the ``RuntimeError`` raised when the retry limit is hit).
    """
    import thetagang.thetagang as tg

    config_path = _write_config(tmp_path, max_startup_retries=max_startup_retries)

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(tg.util, "getLoop", lambda: loop)
    monkeypatch.setattr(tg, "need_to_exit", lambda *_: False)

    captured = {}

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
            self.startedEvent = DummyEvent()
            self.stoppedEvent = DummyEvent()
            captured["watchdog"] = self

        def start(self):
            assert asyncio.get_running_loop() is loop
            self.started = True
            for event in event_script:
                if event == "started":
                    self.startedEvent.emit(self)
                elif event == "stopped":
                    self.stoppedEvent.emit(self)
                else:  # pragma: no cover - guards against typos in tests
                    raise ValueError(f"unknown event {event!r}")
            completion_future = captured["completion_future"]
            if not completion_future.done():
                completion_future.set_result(True)

        def stop(self):
            self.stopped = True

    class FakeIB:
        def __init__(self):
            self.connectedEvent = DummyEvent()
            self.RaiseRequestErrors = False

        def run(self, awaitable):
            assert asyncio.iscoroutine(awaitable)
            try:
                loop.run_until_complete(awaitable)
            finally:
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
            # Leave the future pending; the fake watchdog drives completion so
            # tests can exercise the startup-retry handling first.
            captured["completion_future"] = completion_future

    monkeypatch.setattr(tg, "IBC", FakeIBC)
    monkeypatch.setattr(tg, "Watchdog", FakeWatchdog)
    monkeypatch.setattr(tg, "IB", FakeIB)
    monkeypatch.setattr(tg, "PortfolioManager", FakePortfolioManager)
    monkeypatch.setattr(tg, "Contract", FakeContract)

    error = None
    try:
        tg.start(
            str(config_path),
            without_ibc=False,
            dry_run=True,
            auto_approve_migration=False,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the test for asserting
        error = exc

    return captured, error


def test_watchdog_runs_inside_task(monkeypatch, tmp_path):
    captured, error = _run_start(monkeypatch, tmp_path)

    assert error is None
    assert captured["watchdog"].started is True
    assert captured["watchdog"].stopped is True
    assert captured["ibc"].terminated is True
    assert captured["ibc"].twsVersion == 1045


def test_consecutive_startup_failures_give_up(monkeypatch, tmp_path):
    # Three consecutive stops with no successful connect should hit the limit.
    captured, error = _run_start(
        monkeypatch,
        tmp_path,
        max_startup_retries=3,
        event_script=["stopped", "stopped", "stopped"],
    )

    assert isinstance(error, RuntimeError)
    assert "after 3 attempts" in str(error)
    # The watchdog is still torn down cleanly on the way out.
    assert captured["watchdog"].stopped is True
    assert captured["ibc"].terminated is True


def test_fewer_failures_than_limit_then_success(monkeypatch, tmp_path):
    # Two pre-start failures (below the limit of 3) followed by a successful
    # connect must not give up.
    _captured, error = _run_start(
        monkeypatch,
        tmp_path,
        max_startup_retries=3,
        event_script=["stopped", "stopped", "started"],
    )

    assert error is None


def test_zero_retries_preserves_unlimited_retries(monkeypatch, tmp_path):
    # maxStartupRetries = 0 must preserve the original unlimited-retry behavior:
    # no number of startup failures should cause a give-up.
    _captured, error = _run_start(
        monkeypatch,
        tmp_path,
        max_startup_retries=0,
        event_script=["stopped"] * 10,
    )

    assert error is None


def test_default_config_uses_unlimited_retries(monkeypatch, tmp_path):
    # With no override, the default (0) must not give up after repeated stops.
    _captured, error = _run_start(
        monkeypatch,
        tmp_path,
        event_script=["stopped"] * 10,
    )

    assert error is None


def test_started_then_stopped_does_not_count_as_startup_failure(monkeypatch, tmp_path):
    # A successful connect followed by a runtime stop (disconnect/timeout) must
    # not consume a startup retry: here only a single cycle ever fails to start,
    # which is below the limit of 2. Counting the runtime stop would wrongly
    # trip the limit, so this distinguishes the fix from a naive
    # increment-on-every-stop implementation.
    _captured, error = _run_start(
        monkeypatch,
        tmp_path,
        max_startup_retries=2,
        event_script=["started", "stopped", "stopped"],
    )

    assert error is None


def test_runtime_stop_resets_then_startup_failures_count(monkeypatch, tmp_path):
    # A successful connect + runtime stop is not counted, but subsequent
    # consecutive pre-start failures are, and still trip the limit.
    captured, error = _run_start(
        monkeypatch,
        tmp_path,
        max_startup_retries=2,
        event_script=["started", "stopped", "stopped", "stopped"],
    )

    assert isinstance(error, RuntimeError)
    assert "after 2 attempts" in str(error)
    assert captured["watchdog"].stopped is True
