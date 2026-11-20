"""Shared fixtures."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from custom_components.atonstorage import controller as controller_module
from fake_portal import FakePortal


@pytest.fixture
def portal() -> FakePortal:
    return FakePortal()


@pytest.fixture
def mock_portal(portal, enable_custom_integrations):
    """Route the controller's real httpx client to the fake portal."""
    real = controller_module.create_async_httpx_client

    def factory(hass, **kwargs):
        return real(hass, transport=httpx.MockTransport(portal), **kwargs)

    with patch.object(controller_module, "create_async_httpx_client", factory):
        yield portal
