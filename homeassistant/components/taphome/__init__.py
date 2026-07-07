"""TapHome integration."""

from dataclasses import dataclass
import logging

from aiohttp.web import Request
from taphome_sdk import (
    HubConnectionState,
    TapHomeAuthError,
    TapHomeHub,
    TapHomeHubFactory,
)

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.components.fan import DOMAIN as FAN_DOMAIN
from homeassistant.components.humidifier import DOMAIN as HUMIDIFIER_DOMAIN
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.time import DOMAIN as TIME_DOMAIN
from homeassistant.components.valve import DOMAIN as VALVE_DOMAIN
from homeassistant.components.webhook import (
    async_register as async_register_webhook,
    async_unregister as async_unregister_webhook,
)
from homeassistant.const import (
    CONF_BINARY_SENSORS,
    CONF_COVERS,
    CONF_ID,
    CONF_LIGHTS,
    CONF_SENSORS,
    CONF_SWITCHES,
    CONF_TOKEN,
    CONF_WEBHOOK_ID,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .binary_sensor import BinarySensorEntityConfig
from .button import TapHomeButtonConfig
from .climate import TapHomeClimateConfig
from .const import (
    AVAILABLE_ATTRIBUTES,
    CONF_API_URL,
    CONF_BUTTONS,
    CONF_CLIMATES,
    CONF_ENABLED_ATTRIBUTES,
    CONF_FAN,
    CONF_HUMIDIFIER,
    CONF_KNOWN_DEVICE_IDS,
    CONF_LABELS,
    CONF_MULTIVALUE_SWITCHES,
    CONF_NUMBERS,
    CONF_TIMES,
    CONF_VALVE,
    CONF_ZONES,
    DOMAIN,
    PLATFORMS,
    TAPHOME_PLATFORM,
)
from .cover import TapHomeCoverConfig
from .fan import TapHomeFanConfig
from .humidifier import TapHomeHumidifierConfig
from .light import TapHomeLightConfig
from .platform_descriptors import device_config_id
from .sensor import TapHomeSensorConfig
from .switch import TapHomeSwitchConfig
from .taphome_config_entry import (
    AddEntryRequest,
    NameMapping,
    TapHomeCoreConfig,
    TapHomeEntityConfig,
)
from .taphome_data import TapHomeConfigEntry, TapHomeRuntimeData
from .taphome_issue_registry import TapHomeIssueRegistry
from .valve import TapHomeValveConfig

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class DomainDefinition:
    """Bind a Home Assistant platform domain to its TapHome configuration."""

    name: str
    config_key: str
    config_entry_type: type


DOMAIN_DEFINITIONS: tuple[DomainDefinition, ...] = (
    DomainDefinition(
        BINARY_SENSOR_DOMAIN, CONF_BINARY_SENSORS, BinarySensorEntityConfig
    ),
    DomainDefinition(BUTTON_DOMAIN, CONF_BUTTONS, TapHomeButtonConfig),
    DomainDefinition(EVENT_DOMAIN, CONF_BUTTONS, TapHomeButtonConfig),
    DomainDefinition(CLIMATE_DOMAIN, CONF_CLIMATES, TapHomeClimateConfig),
    DomainDefinition(COVER_DOMAIN, CONF_COVERS, TapHomeCoverConfig),
    DomainDefinition(LIGHT_DOMAIN, CONF_LIGHTS, TapHomeLightConfig),
    DomainDefinition(FAN_DOMAIN, CONF_FAN, TapHomeFanConfig),
    DomainDefinition(VALVE_DOMAIN, CONF_VALVE, TapHomeValveConfig),
    DomainDefinition(HUMIDIFIER_DOMAIN, CONF_HUMIDIFIER, TapHomeHumidifierConfig),
    DomainDefinition(SELECT_DOMAIN, CONF_MULTIVALUE_SWITCHES, TapHomeEntityConfig),
    DomainDefinition(SENSOR_DOMAIN, CONF_SENSORS, TapHomeSensorConfig),
    DomainDefinition(SWITCH_DOMAIN, CONF_SWITCHES, TapHomeSwitchConfig),
    DomainDefinition(TIME_DOMAIN, CONF_TIMES, TapHomeEntityConfig),
    DomainDefinition(NUMBER_DOMAIN, CONF_NUMBERS, TapHomeEntityConfig),
)


async def async_setup_entry(hass: HomeAssistant, entry: TapHomeConfigEntry) -> bool:
    """Set up a TapHome core from a config entry."""
    core_config = _build_core_config(entry)
    taphome_issue_registry = TapHomeIssueRegistry(hass, core_config.id)

    try:
        hub = await TapHomeHubFactory.async_connect(
            entry.data[CONF_API_URL],
            entry.data[CONF_TOKEN],
            async_get_clientsession(hass),
        )
    except TapHomeAuthError as error:
        raise ConfigEntryAuthFailed(
            "TapHome API rejected the configured token"
        ) from error
    except Exception as error:
        raise ConfigEntryNotReady(
            f"Failed to connect to TapHome core: {error}"
        ) from error

    if hub.connection_state.value is not HubConnectionState.CONNECTED:
        hub.disconnect()
        raise ConfigEntryNotReady("TapHome hub is not connected")

    def hub_connection_state_changed(
        _: HubConnectionState, state: HubConnectionState
    ) -> None:
        """Handle changes in hub connection state."""
        if state is HubConnectionState.CONNECTED:
            taphome_issue_registry.try_delete_core_unavailable_issue()
        else:
            taphome_issue_registry.create_core_unavailable_issue()

    hub.connection_state.changed += hub_connection_state_changed

    _register_hub_device(hass, entry, hub)
    _register_webhook(hass, entry, hub, core_config)
    _async_remove_stale_entities(hass, entry)
    _async_detect_new_devices(hass, entry, hub, taphome_issue_registry)

    add_entry_requests = {
        domain.name: _map_add_entry_requests(
            hass,
            core_config,
            _map_config_entries(
                domain.config_entry_type, entry.options.get(domain.config_key, [])
            ),
            hub,
        )
        for domain in DOMAIN_DEFINITIONS
    }

    entry.runtime_data = TapHomeRuntimeData(
        hub, core_config, add_entry_requests, hub_connection_state_changed
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: TapHomeConfigEntry) -> bool:
    """Unload a TapHome config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime_data = entry.runtime_data
        runtime_data.hub.connection_state.changed -= (
            runtime_data.connection_state_handler
        )
        runtime_data.hub.disconnect()
    return unload_ok


async def async_migrate_entry(_hass: HomeAssistant, _entry: TapHomeConfigEntry) -> bool:
    """Migrate old config entries to the current version."""
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: TapHomeConfigEntry
) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _build_core_config(entry: TapHomeConfigEntry) -> TapHomeCoreConfig:
    """Build the immutable core configuration from a config entry."""
    options = entry.options
    zone_mapping = (
        NameMapping.from_dict(options.get(CONF_ZONES))
        if CONF_ZONES in options
        else None
    )
    label_mapping = (
        NameMapping.from_dict(options.get(CONF_LABELS))
        if CONF_LABELS in options
        else None
    )

    return TapHomeCoreConfig(
        entry.data.get(CONF_ID),
        zone_mapping,
        label_mapping,
        tuple(options.get(CONF_ENABLED_ATTRIBUTES, AVAILABLE_ATTRIBUTES)),
    )


def _parse_device_id_from_unique_id(unique_id: str) -> int | None:
    """Extract the TapHome device id from a generated entity unique id."""
    if not unique_id.startswith("taphome"):
        return None
    try:
        return int(unique_id.rsplit(".", 1)[-1])
    except ValueError:
        return None


def _async_remove_stale_entities(
    hass: HomeAssistant, entry: TapHomeConfigEntry
) -> None:
    """Remove registry entities whose device is no longer configured."""
    configured_ids: dict[str, set[int]] = {}
    for domain in DOMAIN_DEFINITIONS:
        ids = configured_ids.setdefault(domain.name, set())
        for device_config in entry.options.get(domain.config_key) or []:
            if isinstance(device_config, dict):
                ids.add(int(device_config["id"]))
            else:
                ids.add(int(device_config))

    entity_registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    ):
        if registry_entry.domain not in configured_ids:
            continue
        # Anything unparseable is left alone to stay on the safe side.
        device_id = _parse_device_id_from_unique_id(registry_entry.unique_id)
        if device_id is None:
            continue
        if device_id not in configured_ids[registry_entry.domain]:
            entity_registry.async_remove(registry_entry.entity_id)


def _all_configured_device_ids(entry: TapHomeConfigEntry) -> set[int]:
    """Return the ids of every device configured on any platform."""
    return {
        device_config_id(device_config)
        for domain in DOMAIN_DEFINITIONS
        for device_config in entry.options.get(domain.config_key) or []
    }


def _async_detect_new_devices(
    hass: HomeAssistant,
    entry: TapHomeConfigEntry,
    hub: TapHomeHub,
    issue_registry: TapHomeIssueRegistry,
) -> None:
    """Report devices exposed since the entry was last known about.

    On the first setup the currently exposed devices become the baseline, so
    nothing is reported. Afterwards any device exposed in the API that is not in
    the baseline and not already configured raises a fixable repair issue.
    """
    exposed = set(hub.devices)
    configured = _all_configured_device_ids(entry)
    known_raw = entry.data.get(CONF_KNOWN_DEVICE_IDS)

    if known_raw is None:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_KNOWN_DEVICE_IDS: sorted(exposed | configured)},
        )
        return

    known = {int(value) for value in known_raw}
    new_devices = exposed - known - configured
    issue_registry.sync_new_device_issues(entry.entry_id, hub, new_devices)


def _register_hub_device(
    hass: HomeAssistant, entry: TapHomeConfigEntry, hub: TapHomeHub
) -> None:
    """Register the TapHome Core as the hub device."""
    location = hub.location
    location_id = (
        location.location_id if location else entry.data.get(CONF_ID) or DOMAIN
    )
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, location_id)},
        name=location.location_name if location else entry.title,
        manufacturer="TapHome",
        model="Core",
    )


def _register_webhook(
    hass: HomeAssistant,
    entry: TapHomeConfigEntry,
    hub: TapHomeHub,
    core_config: TapHomeCoreConfig,
) -> None:
    """Register the push webhook for the entry when configured."""
    webhook_id = entry.options.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        return

    webhook_name = f"Taphome-{core_config.id}" if core_config.id else "Taphome"

    async def async_handle_webhook(
        _: HomeAssistant, webhook_id: str, request: Request
    ) -> None:
        _LOGGER.info("Taphome webhook triggered - webhook_id: %s", webhook_id)
        try:
            payload = await request.json()
        except ValueError:
            _LOGGER.warning("Ignoring TapHome webhook with an invalid JSON body")
            return
        await hub.async_handle_webhook(payload)

    async_register_webhook(
        hass, TAPHOME_PLATFORM, webhook_name, webhook_id, async_handle_webhook
    )
    entry.async_on_unload(lambda: async_unregister_webhook(hass, webhook_id))


def _map_config_entries(
    config_entry_factory, platform_config: list
) -> list[TapHomeEntityConfig]:
    return list(map(config_entry_factory, platform_config))


def _map_add_entry_requests(
    hass: HomeAssistant,
    core_config_entry: TapHomeCoreConfig,
    config_entries: list[TapHomeEntityConfig],
    hub: TapHomeHub,
) -> list[AddEntryRequest]:
    return [
        AddEntryRequest(hass, core_config_entry, config_entry, hub)
        for config_entry in config_entries
    ]
