"""AtonStorage integration."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .controller import Controller as AtonStorage

_LOGGER = logging.getLogger(__name__)


@dataclass
class AtonStorageBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Class to describe a AtonStorage sensor entity."""

    value_calc_function: Callable[[AtonStorage], Any] = None
    # Unknown on a plant without a wallbox, where every EV field reads -1.
    requires_wallbox: bool = False
    # A live reading, unavailable once the plant stops reporting.
    instant: bool = False


INVERTER_BINARY_SENSOR_DESCRIPTIONS = (
    AtonStorageBinarySensorEntityDescription(
        key="grid_to_house",
        translation_key="grid_to_house",
        instant=True,
        name="Grid to House",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.grid_to_house,
    ),
    AtonStorageBinarySensorEntityDescription(
        key="solar_to_battery",
        translation_key="solar_to_battery",
        instant=True,
        name="Solar to Battery",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.solar_to_battery,
    ),
    AtonStorageBinarySensorEntityDescription(
        key="solar_to_grid",
        translation_key="solar_to_grid",
        instant=True,
        name="Solar to Grid",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.solar_to_grid,
    ),
    AtonStorageBinarySensorEntityDescription(
        key="battery_to_house",
        translation_key="battery_to_house",
        instant=True,
        name="Battery to House",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.battery_to_house,
    ),
    AtonStorageBinarySensorEntityDescription(
        key="solar_to_house",
        translation_key="solar_to_house",
        instant=True,
        name="Solar to House",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.solar_to_house,
    ),
    AtonStorageBinarySensorEntityDescription(
        key="grid_to_battery",
        translation_key="grid_to_battery",
        instant=True,
        name="Grid to Battery",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.grid_to_battery,
    ),
    AtonStorageBinarySensorEntityDescription(
        key="battery_to_grid",
        translation_key="battery_to_grid",
        instant=True,
        name="Battery to Grid",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.battery_to_grid,
    ),
    # EV
    AtonStorageBinarySensorEntityDescription(
        key="ev_status_off",
        translation_key="ev_status_off",
        requires_wallbox=True,
        instant=True,
        name="EV OFF",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.ev_status_off,
    ),
    AtonStorageBinarySensorEntityDescription(
        key="ev_status_on",
        translation_key="ev_status_on",
        requires_wallbox=True,
        instant=True,
        name="EV ON",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.ev_status_on,
    ),
    AtonStorageBinarySensorEntityDescription(
        key="ev_status_charge",
        translation_key="ev_status_charge",
        requires_wallbox=True,
        instant=True,
        name="EV Charging",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.ev_status_charge,
    ),
    AtonStorageBinarySensorEntityDescription(
        key="ev_status_warning",
        translation_key="ev_status_warning",
        requires_wallbox=True,
        instant=True,
        name="EV Warning",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: controller.ev_status_warning,
    ),
    # ADDED IN 1.0.12
    AtonStorageBinarySensorEntityDescription(
        key="wifi",
        translation_key="wifi",
        name="WiFi",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: str(
            (controller.monitor_data or {}).get("wifi", "0")
        )
        not in ("0", "", "None"),
    ),
    AtonStorageBinarySensorEntityDescription(
        key="has_alarms",
        translation_key="has_alarms",
        name="Alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_calc_function=lambda controller: bool(controller.active_alarms),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AtonStorage sensors."""
    _LOGGER.debug("Set up the AtonStorage binary sensors")
    entities = _create_entities(hass, entry)
    # No update_before_add: the coordinator has just refreshed, and asking again
    # here triggered two more full refreshes, the second one rate limited.
    async_add_entities(entities)


def _create_entities(hass: HomeAssistant, entry: dict):
    entities = []

    controller = hass.data[DOMAIN][entry.entry_id]["controller"]
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    username = hass.data[DOMAIN][entry.entry_id]["username"]
    sensors_selected = hass.data[DOMAIN][entry.entry_id]["sensors_selected"]

    if "BINARY SENSORS" in sensors_selected:
        for entity_description in INVERTER_BINARY_SENSOR_DESCRIPTIONS:
            entities.append(
                AtonStorageBinarySensorEntity(
                    entry=entry,
                    controller=controller,
                    coordinator=coordinator,
                    description=entity_description,
                    username=username,
                )
            )

    return entities


class AtonStorageBinarySensorEntity(CoordinatorEntity, BinarySensorEntity):
    """AtonStorage Sensor which receives its data via an DataUpdateCoordinator."""

    entity_description: AtonStorageBinarySensorEntityDescription
    # _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        controller: AtonStorage,
        coordinator,
        description: AtonStorageBinarySensorEntityDescription,
        username,
        # device_info,
    ):
        """Batched AtonStorage Sensor Entity constructor."""
        super().__init__(coordinator)

        self.controller = controller
        self.entity_description = description

        # self._entry = entry
        # self._name = self.entity_description.name
        # self._attr_name = f"{controller.serial_number}_{self.entity_description.name}"
        # self._attr_translation_key = self.entity_description.key
        self._attr_name = f"{username} {self.entity_description.name}"
        self._attr_unique_id = (
            f"{controller.serial_number}_{self.entity_description.key}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "AtonStorage " + username)},
            name=username,
            manufacturer="AtonStorage",
            sw_version=controller.fw_Scheda,
            serial_number=controller.serial_number,
        )

        self._register_key = self.entity_description.key

    @property
    def available(self) -> bool:
        """Live readings go unavailable once the plant stops reporting."""
        if not super().available:
            return False
        return not (self.entity_description.instant and self.controller.is_stale)

    @property
    def is_on(self):
        """Return true if the binary sensor is on."""

        description = self.entity_description
        if description.requires_wallbox and not self.controller.has_wallbox:
            return None
        try:
            return description.value_calc_function(self.controller)
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.debug("No usable value for %s: %s", description.key, err)
            return None
