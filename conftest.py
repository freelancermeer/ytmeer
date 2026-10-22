"""Shared pytest configuration.

Only the marker registration: the `network` tests in test_mcp_server.py talk to
YouTube, so they are deselected by default and pytest warns about an unknown
mark unless it is declared somewhere it reads at startup - which a hook inside
a test module is not.

    python3 -m pytest -q                      # offline
    python3 -m pytest -q -m network           # only the ones that hit YouTube
"""


def pytest_configure(config):
    config.addinivalue_line("markers", "network: talks to YouTube over the network")


def pytest_collection_modifyitems(config, items):
    import pytest
    if config.getoption("-m"):            # an explicit -m is the caller's choice
        return
    skip = pytest.mark.skip(reason="needs the network; run with -m network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
