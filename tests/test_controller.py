import pytest

from conftest import ROOT
from terminal.config import load_config
from terminal.controller import Controller


class Session:
    logged_in = True


def test_controller_requires_authenticated_angel_one_session():
    cfg = load_config(ROOT / "config.yaml")
    session = Session()
    session.logged_in = False
    controller = Controller(cfg, lambda: pytest.fail("engine must not be built"), session)
    with pytest.raises(RuntimeError, match="Connect your Angel One account"):
        controller.start()


def test_controller_starts_after_angel_one_authentication_even_with_legacy_static_ip_set():
    cfg = load_config(ROOT / "config.yaml")
    cfg.static_ip = "198.51.100.9"  # retained only for old config compatibility
    built = []

    class Engine:
        finished = type("Event", (), {"is_set": staticmethod(lambda: False)})()
        def start(self):
            built.append(True)

    controller = Controller(cfg, Engine, Session())
    controller.start()
    assert built == [True]


def test_sample_controller_can_run_without_a_broker_session():
    cfg = load_config(ROOT / "config.yaml")
    built = []

    class Engine:
        finished = type("Event", (), {"is_set": staticmethod(lambda: False)})()
        def start(self):
            built.append(True)

    Controller(cfg, Engine).start()
    assert built == [True]
