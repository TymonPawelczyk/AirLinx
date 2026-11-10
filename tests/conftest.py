"""Shared pytest config. Markers and default deselection live in pytest.ini."""
import os

import pytest


def pytest_collection_modifyitems(config, items):
    """tv tests need AIRLINX_TV_IP even when selected with -m tv."""
    if os.environ.get("AIRLINX_TV_IP"):
        return
    skip = pytest.mark.skip(reason="set AIRLINX_TV_IP to run live TV tests")
    for item in items:
        if item.get_closest_marker("tv"):
            item.add_marker(skip)
