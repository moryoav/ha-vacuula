"""Exercise bounded mDNS browsing without opening network sockets."""

import asyncio
from ipaddress import ip_address
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from zeroconf import BadTypeInNameException, NotRunningException, ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from custom_components.vacuula import discovery


def service(name="Office", host="192.0.2.2", protocol="http"):
    """Build the same real records decoded by Home Assistant's discovery helper."""
    return AsyncServiceInfo(
        discovery.SERVICE_TYPE,
        f"{name}.{discovery.SERVICE_TYPE}",
        addresses=[ip_address(host).packed] if host else [],
        port=8125,
        server="vacuula.local.",
        properties={b"protocol": protocol.encode()},
    )


@pytest.fixture
def network():
    """Keep the shared instance and browser mocked while decoding actual records."""
    state = SimpleNamespace(
        instance=Mock(),
        browser=Mock(async_cancel=AsyncMock()),
        records={},
        names=[],
        handler=None,
    )

    def create_browser(instance, service_type, *, handlers):
        """Deliver configured Added events through the browser's captured handler."""
        assert instance is state.instance
        assert service_type == discovery.SERVICE_TYPE
        state.handler = handlers[0]
        for name in state.names:
            state.handler(
                zeroconf=instance,
                service_type=service_type,
                name=name,
                state_change=ServiceStateChange.Added,
            )
        return state.browser

    with (
        patch.object(
            discovery.zeroconf,
            "async_get_instance",
            new_callable=AsyncMock,
            return_value=state.instance,
        ),
        patch.object(
            discovery, "AsyncServiceBrowser", side_effect=create_browser
        ) as browser,
        patch.object(
            discovery,
            "AsyncServiceInfo",
            side_effect=lambda service_type, name: state.records[name],
        ) as info,
        patch.object(
            AsyncServiceInfo, "async_request", new_callable=AsyncMock, return_value=True
        ) as request,
        patch.object(discovery, "DISCOVERY_TIMEOUT", 0),
    ):
        state.browser_constructor = browser
        state.info_constructor = info
        state.request = request
        yield state
    state.instance.close.assert_not_called()
    state.instance.async_close.assert_not_called()


async def test_dynamic_names_and_protocols(hass, network):
    """Browse every advertised instance and preserve advertised address and port."""
    records = [service("Upstairs", "2001:db8::2", "https"), service("Office")]
    network.names = [record.name for record in records]
    network.records = {record.name: record for record in records}

    result = await discovery.async_discover_servers(hass)

    assert [item.name for item in result] == sorted(network.names)
    assert result[0].host == "192.0.2.2"
    assert result[1].host == "2001:db8::2"
    assert result[1].properties == {"protocol": "https"}
    assert result[1].port == 8125
    assert network.request.await_count == 2
    network.request.assert_awaited_with(network.instance, discovery.RESOLVE_TIMEOUT_MS)
    network.browser.async_cancel.assert_awaited_once()


async def test_no_servers(hass, network):
    """A completed empty browse allows the caller to offer manual input."""
    assert await discovery.async_discover_servers(hass) == []
    network.info_constructor.assert_not_called()
    network.browser.async_cancel.assert_awaited_once()


async def test_duplicate_updates_resolve_once(hass, network):
    """Repeated cached advertisements do not create duplicate resolver tasks."""
    record = service()
    network.names = [record.name, record.name]
    network.records = {record.name: record}

    assert len(await discovery.async_discover_servers(hass)) == 1
    network.info_constructor.assert_called_once()


@pytest.mark.parametrize("host", [None, "fe80::1", "0.0.0.0"])
async def test_unusable_addresses(hass, network, host):
    """Use Home Assistant's address rules rather than guessing a reachable host."""
    record = service(host=host)
    network.names = [record.name]
    network.records = {record.name: record}
    assert await discovery.async_discover_servers(hass) == []


@pytest.mark.parametrize(
    "failure",
    [OSError("offline"), NotRunningException(), BadTypeInNameException(), ValueError()],
)
async def test_resolution_errors(hass, network, failure):
    """A broken advertisement does not prevent manual setup."""
    record = service()
    network.names = [record.name]
    network.records = {record.name: record}
    network.request.side_effect = failure
    assert await discovery.async_discover_servers(hass) == []


async def test_unresolved_record(hass, network):
    """An mDNS resolution timeout does not create an incomplete candidate."""
    record = service()
    network.names = [record.name]
    network.records = {record.name: record}
    network.request.return_value = False
    assert await discovery.async_discover_servers(hass) == []


@pytest.mark.parametrize("failure", [OSError("offline"), NotRunningException()])
async def test_browser_unavailable(hass, network, failure):
    """Unavailable multicast falls back without closing Home Assistant's instance."""
    network.browser_constructor.side_effect = failure
    assert await discovery.async_discover_servers(hass) == []


async def test_shared_instance_unavailable(hass, network):
    """A network failure before browser construction also falls back cleanly."""
    with patch.object(
        discovery.zeroconf, "async_get_instance", side_effect=OSError("offline")
    ):
        assert await discovery.async_discover_servers(hass) == []
    network.browser_constructor.assert_not_called()


async def test_removed_record(hass, network):
    """Remove a server which disappears after its details were resolved."""
    record = service()
    network.names = [record.name]
    network.records = {record.name: record}
    original_sleep = asyncio.sleep

    async def remove_after_resolution(timeout):
        """Yield to resolution before emitting the server's Removed event."""
        await original_sleep(0)
        network.handler(
            zeroconf=network.instance,
            service_type=discovery.SERVICE_TYPE,
            name=record.name,
            state_change=ServiceStateChange.Removed,
        )

    with patch.object(discovery.asyncio, "sleep", side_effect=remove_after_resolution):
        assert await discovery.async_discover_servers(hass) == []


async def test_unrelated_service_ignored(hass, network):
    """Do not resolve events for a different service type."""
    original_constructor = network.browser_constructor.side_effect

    def create_browser(*args, **kwargs):
        """Inject unrelated and nonexistent removal events into normal browsing."""
        browser = original_constructor(*args, **kwargs)
        network.handler(
            zeroconf=network.instance,
            service_type="_unrelated._tcp.local.",
            name="Other._unrelated._tcp.local.",
            state_change=ServiceStateChange.Added,
        )
        network.handler(
            zeroconf=network.instance,
            service_type=discovery.SERVICE_TYPE,
            name="Missing._vacuula._tcp.local.",
            state_change=ServiceStateChange.Removed,
        )
        return browser

    network.browser_constructor.side_effect = create_browser
    assert await discovery.async_discover_servers(hass) == []
    network.info_constructor.assert_not_called()


async def test_removed_pending_resolution_drained(hass, network):
    """A removed pending resolver still finishes cancellation before returning."""
    record = service()
    network.names = [record.name]
    network.records = {record.name: record}
    original_sleep = asyncio.sleep
    cancelled = asyncio.Event()

    async def never_resolves(*args):
        """Wait for cancellation and mark completion after asynchronous cleanup."""
        try:
            await asyncio.Event().wait()
        finally:
            await original_sleep(0)
            cancelled.set()

    async def remove_pending(timeout):
        """Remove the advertised server while its resolver is still pending."""
        await original_sleep(0)
        network.handler(
            zeroconf=network.instance,
            service_type=discovery.SERVICE_TYPE,
            name=record.name,
            state_change=ServiceStateChange.Removed,
        )

    network.request.side_effect = never_resolves
    with patch.object(discovery.asyncio, "sleep", side_effect=remove_pending):
        assert await discovery.async_discover_servers(hass) == []
    assert cancelled.is_set()


async def test_pending_resolution_cancelled(hass, network):
    """The browse deadline also bounds outstanding resolution and drains cleanup."""
    record = service()
    network.names = [record.name]
    network.records = {record.name: record}
    cancelled = asyncio.Event()

    async def never_resolves(*args):
        """Leave resolution pending until the browse deadline cancels it."""
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    network.request.side_effect = never_resolves
    assert await discovery.async_discover_servers(hass) == []
    assert cancelled.is_set()
    network.browser.async_cancel.assert_awaited_once()


async def test_flow_cancelled(hass, network):
    """Cancelling setup closes only this browser and drains resolver tasks."""
    record = service()
    network.names = [record.name]
    network.records = {record.name: record}
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def never_resolves(*args):
        """Signal resolver startup and record cleanup when setup is cancelled."""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    network.request.side_effect = never_resolves
    with patch.object(discovery, "DISCOVERY_TIMEOUT", 60):
        task = asyncio.create_task(discovery.async_discover_servers(hass))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set()
    network.browser.async_cancel.assert_awaited_once()


async def test_browser_shutdown_failure(hass, network):
    """A stopped shared socket during browser cleanup still permits manual input."""
    network.browser.async_cancel.side_effect = NotRunningException()
    assert await discovery.async_discover_servers(hass) == []
