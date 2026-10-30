"""Read-only enrollment, curtailment and fault indicators."""

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .ev_sensor import OptimiseEVEntity, OptimiseEVModuleEntity, setup_dynamic_vehicles
from .sensor import OptimiseEntity, _data


@dataclass(frozen=True, slots=True)
class BinarySpec:
    key: str
    field: str
    name: str
    section: str = "status"
    device_class: BinarySensorDeviceClass | None = None


BINARY_SENSORS = (
    BinarySpec("battery_enrolled", "battery_enrolled", "Battery enrolled"),
    BinarySpec("solar_enrolled", "solar_enrolled", "Solar enrolled"),
    BinarySpec("ev_enrolled", "ev_enrolled", "Legacy EV enrolled"),
    BinarySpec("ev_devices_enrolled", "ev_devices_enrolled", "EV devices enrolled"),
    BinarySpec(
        "global_killswitch",
        "global_killswitch",
        "Global automation stop",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BinarySpec("curtailment_enabled", "curtailment_enabled", "Solar curtailment enabled"),
    BinarySpec("curtailment_supported", "curtailment_supported", "Solar curtailment supported"),
    BinarySpec(
        "device_error", "has_error", "Device problem", device_class=BinarySensorDeviceClass.PROBLEM
    ),
    BinarySpec(
        "curtailment_active", "isSolarCurtailmentActive", "Solar curtailment active", "live"
    ),
)


class OptimiseBinarySensor(OptimiseEntity, BinarySensorEntity):
    def __init__(self, coordinator, site_label, description):
        super().__init__(coordinator, site_label, description.key)
        self.description = description
        self.entity_description = BinarySensorEntityDescription(
            key=description.key, name=description.name
        )
        self._attr_device_class = description.device_class

    @property
    def available(self):
        return super().available and isinstance(
            _data(self.coordinator).get(self.description.field), bool
        )

    @property
    def is_on(self):
        return _data(self.coordinator).get(self.description.field) if self.available else None


class OptimiseEVReadyBySensor(OptimiseEVEntity, BinarySensorEntity):
    """Provider-reported ready-by eligibility, never charging or plug inference."""

    def __init__(self, coordinator, site_label, vehicle, number):
        super().__init__(coordinator, site_label, vehicle, number, "ready_by_eligible")
        self.entity_description = BinarySensorEntityDescription(
            key="ev_ready_by_eligible", name="Ready-by eligible"
        )

    @property
    def available(self):
        row = self._vehicle()
        return super().available and isinstance(row.get("ready_by_eligible"), bool)

    @property
    def is_on(self):
        return self._vehicle()["ready_by_eligible"] if self.available else None


class OptimiseEVReadyByConfigSensor(OptimiseEVModuleEntity, BinarySensorEntity):
    """An explicit provider flag; never inferred from a target/cancellation time."""

    def __init__(self, coordinator, site_label, vehicle, number, field):
        super().__init__(coordinator, site_label, vehicle, number, f"plan_{field}", "ready_by")
        self.field = field
        self.entity_description = BinarySensorEntityDescription(
            key=f"ev_plan_{field}", name=f"Ready-by {field}"
        )

    @property
    def available(self):
        return super().available and isinstance(self._module().get(self.field), bool)

    @property
    def is_on(self):
        return self._module()[self.field] if self.available else None


async def async_setup_entry(hass, entry, async_add_entities):
    entities = []
    for index, (_site_id, site) in enumerate(entry.runtime_data.sites.items(), 1):
        descriptions = BINARY_SENSORS
        entities.extend(
            OptimiseBinarySensor(getattr(site, spec.section), f"Optimise site {index}", spec)
            for spec in descriptions
        )
    async_add_entities(entities)
    for index, site in enumerate(entry.runtime_data.sites.values(), 1):
        setup_dynamic_vehicles(
            entry,
            site.ev,
            lambda vehicle, number, site=site, label=f"Optimise site {index}": [
                OptimiseEVReadyBySensor(site.ev, label, vehicle, number),
                OptimiseEVReadyByConfigSensor(site.ev_plan, label, vehicle, number, "active"),
                OptimiseEVReadyByConfigSensor(site.ev_plan, label, vehicle, number, "recurring"),
            ],
            async_add_entities,
        )
