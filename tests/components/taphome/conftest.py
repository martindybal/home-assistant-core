"""Shared fixtures for the TapHome integration tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from taphome_sdk import HubConnectionState, TapHomeApi, TapHomeHubFactory

from . import TEST_LOCATION, make_config_entry, make_hub

from tests.common import MockConfigEntry


@pytest.fixture
def mock_hub():
    """Patch the SDK connection points and yield the connected hub."""
    hub = make_hub()

    async def _connect(*args, **kwargs):
        # A reload disconnects the hub; reconnect it like the real factory.
        hub.connection_state.value = HubConnectionState.CONNECTED
        return hub

    with (
        patch.object(
            TapHomeHubFactory, "async_connect", AsyncMock(side_effect=_connect)
        ) as connect,
        patch.object(
            TapHomeApi, "async_get_location", AsyncMock(return_value=TEST_LOCATION)
        ) as get_location,
    ):
        hub.mock_connect = connect
        hub.mock_get_location = get_location
        yield hub


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry as created by the config flow."""
    return make_config_entry()
