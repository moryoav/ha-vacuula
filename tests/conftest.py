"""Home Assistant test setup."""

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations, mock_zeroconf, mock_async_zeroconf
):
    """Load the local integration while keeping its mDNS dependency off the LAN."""
    yield
