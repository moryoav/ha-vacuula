"""Keep action texts translatable instead of fixed in services.yaml."""

import json
from pathlib import Path

import yaml
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

from custom_components.vacuula.const import DOMAIN

ROOT = Path(__file__).parents[1] / "custom_components" / DOMAIN


def load_services():
    """Read the action definitions as Home Assistant loads them."""
    return yaml.safe_load((ROOT / "services.yaml").read_text(encoding="utf-8"))


def load_strings():
    """Read the source strings used for translations."""
    return json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))


def test_services_yaml_has_no_fixed_texts():
    """Leave names and descriptions to the translation files."""
    for service, spec in load_services().items():
        assert "name" not in spec and "description" not in spec, service
        for field, field_spec in spec["fields"].items():
            assert "name" not in field_spec, f"{service}.{field}"
            assert "description" not in field_spec, f"{service}.{field}"


def test_every_action_field_and_option_is_translated():
    """Name each action and field, and label every dropdown option."""
    services = load_services()
    strings = load_strings()
    assert set(services) == set(strings["services"])
    for service, spec in services.items():
        texts = strings["services"][service]
        assert texts["name"] and texts["description"]
        assert set(spec["fields"]) == set(texts["fields"])
        for field, field_spec in spec["fields"].items():
            assert texts["fields"][field]["name"]
            if select := field_spec["selector"].get("select"):
                labels = strings["selector"][select["translation_key"]]["options"]
                assert set(select["options"]) <= set(labels), f"{service}.{field}"
    english = ROOT / "translations" / "en.json"
    assert json.loads(english.read_text(encoding="utf-8")) == strings


async def test_home_assistant_loads_action_translations(hass):
    """Serve action names and option labels from the English translation."""
    assert await async_setup_component(hass, DOMAIN, {})
    services = await async_get_translations(hass, "en", "services", [DOMAIN])
    selector = await async_get_translations(hass, "en", "selector", [DOMAIN])
    assert services[f"component.{DOMAIN}.services.clean.name"] == (
        "Clean a map or zone"
    )
    assert (
        selector[f"component.{DOMAIN}.selector.cleaning_type.options.house"]
        == "Whole home"
    )
    for service in hass.services.async_services_for_domain(DOMAIN):
        assert f"component.{DOMAIN}.services.{service}.name" in services
