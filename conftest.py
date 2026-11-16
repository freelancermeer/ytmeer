"""Shared pytest configuration.

The marker registration: the `network` tests in test_mcp_server.py talk to
YouTube, so they are deselected by default and pytest warns about an unknown
mark unless it is declared somewhere it reads at startup - which a hook inside
a test module is not.

    python3 -m pytest -q                      # offline
    python3 -m pytest -q -m network           # only the ones that hit YouTube

And one fixture every test gets: api.py keeps answers for 15 minutes and
pauses every lookup after a bot check, both process-wide, so without a reset
one test's stubbed answer would come back as the next test's cache hit.
"""

import sys

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "network: talks to YouTube over the network")


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m"):            # an explicit -m is the caller's choice
        return
    skip = pytest.mark.skip(reason="needs the network; run with -m network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


def _fresh_engine_state():
    api = sys.modules.get("api")
    if api is not None and hasattr(api, "CACHE"):
        api.CACHE.clear()
        api.reset_bot_pause()


@pytest.fixture(autouse=True)
def fresh_engine_state():
    _fresh_engine_state()
    yield
    _fresh_engine_state()
