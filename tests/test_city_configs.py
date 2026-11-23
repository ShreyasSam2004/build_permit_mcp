from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from permits_mcp.city_config import CityConfig, load_cities

CHICAGO = Path(__file__).parents[1] / "src" / "permits_mcp" / "cities" / "chicago.toml"


def _chicago_raw() -> dict:
    with CHICAGO.open("rb") as fh:
        return tomllib.load(fh)


def test_every_packaged_city_loads(cities: dict[str, CityConfig]) -> None:
    assert "chicago" in cities
    for city_id, config in cities.items():
        assert config.id == city_id
        assert config.fields.permit_id in config.mapped_columns()


def test_chicago_keeps_the_en_dash(cities: dict[str, CityConfig]) -> None:
    """The live value uses U+2013. An editor 'fixing' it to a hyphen would silently send
    327k express permits to 'unknown'."""
    values = [v for rule in cities["chicago"].permit_type.rules for vals in rule.when.values() for v in vals]
    assert "PERMIT – EXPRESS PERMIT PROGRAM" in values


def test_chicago_unavailable_fields(cities: dict[str, CityConfig]) -> None:
    assert cities["chicago"].unavailable_fields() == ["last_activity_date"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["fields"].update(permit_id="permit_; DROP"), "invalid column name"),
        (lambda d: d.update(timezone="Mars/Olympus"), "unknown timezone"),
        (lambda d: d["fields"].update(source_url={"template": "https://x/"}), "{permit_id}"),
        (lambda d: d.update(stauts={}), "Extra inputs are not permitted"),
        (lambda d: d["status"]["rules"][0].update(then="open"), "Input should be"),
        (lambda d: d["status"]["rules"][0]["when"].update(permit_status=[]), "lists no values"),
        (lambda d: d.update(dataset_id="ydr8_5enu"), "String should match pattern"),
    ],
)
def test_bad_configs_are_rejected(mutate, message: str) -> None:
    raw = _chicago_raw()
    mutate(raw)
    with pytest.raises(ValidationError, match=message):
        CityConfig.model_validate(raw)


def test_file_name_must_match_id(tmp_path: Path) -> None:
    (tmp_path / "springfield.toml").write_text(CHICAGO.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="must match the file name"):
        load_cities(tmp_path)
