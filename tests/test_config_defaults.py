# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import dataclasses

import yaml

from kiwi_scan import datamodels
from kiwi_scan.cli import config_defaults


def _field_names(cls):
    return [f.name for f in dataclasses.fields(cls)]


def test_render_is_valid_yaml_with_all_scanconfig_fields():
    data = yaml.safe_load(config_defaults.render())
    assert list(data) == _field_names(datamodels.ScanConfig)


def test_render_uses_dataclass_defaults():
    data = yaml.safe_load(config_defaults.render())
    defaults = datamodels.ScanConfig(actuators={}, detector_pvs=[])
    for name in ("data_dir", "output_file", "sample_rate_hz", "manifest_mode",
                 "detector_reader_strategy", "detector_pvs_monitor"):
        assert data[name] == getattr(defaults, name)


def test_render_expands_nested_configs():
    data = yaml.safe_load(config_defaults.render())
    actuator = data["actuators"][config_defaults.EXAMPLE_KEY]
    assert list(actuator) == _field_names(datamodels.ActuatorConfig)
    assert list(actuator["jog"]) == _field_names(datamodels.JogConfig)
    assert list(data["scan_dimensions"][0]) == _field_names(datamodels.ScanDimension)
    assert list(data["triggers"]) == _field_names(datamodels.ScanTriggers)
    assert list(data["triggers"]["before"][0]) == _field_names(datamodels.TriggerAction)
    assert list(data["subscriptions"][0]) == _field_names(datamodels.SubscriptionConfig)
    assert list(data["plugin_configs"][0]) == _field_names(datamodels.PluginConfig)
    assert data["monitor"] == {}


def test_render_follows_new_fields():
    @dataclasses.dataclass
    class Inner:
        a: int
        b: str = "x"

    @dataclasses.dataclass
    class Outer:
        inner: Inner
        items: list = dataclasses.field(default_factory=list)

    data = yaml.safe_load(config_defaults.render(Outer))
    assert data == {"inner": {"a": 0, "b": "x"}, "items": []}


def test_main_writes_output(tmp_path, capsys):
    out = tmp_path / "defaults.yaml"
    assert config_defaults.main(["ActuatorConfig", "-o", str(out)]) == 0
    assert list(yaml.safe_load(out.read_text())) == _field_names(datamodels.ActuatorConfig)
    assert capsys.readouterr().out == ""
