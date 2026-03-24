"""
tests/conftest.py — pytest fixtures shared across all smoke test modules
"""

import pytest
from tests.helpers import GM_BASE, GMClient


@pytest.fixture(scope="session")
def gm_client() -> GMClient:
    """
    A single GMClient for the whole test session.
    Verifies the server is reachable before any tests run.
    """
    client = GMClient(GM_BASE)
    client.check_reachable()
    return client
