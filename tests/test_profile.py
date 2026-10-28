import pytest

from pathlib import Path

from hart_sim import units
from hart_sim.profile import Profile, ProfileError, bundled_profiles, load_profile


def _pressure_text():
    return (Path(units.__file__).parent / "profiles" / "pressure.yaml").read_text()


def test_bundled_profiles_load():
    assert bundled_profiles() == ["pressure", "temperature"]
    p = load_profile("pressure")
    assert isinstance(p, Profile)
    assert p.pv.unit == 12
    assert p.range == (0, 1000)
    assert p.sensor_limits == (-100, 2500)
    assert p.damping == 0.5 and p.min_span == 10 and p.alarm == "high"
    assert p.device_type == 0xFA01 and p.manufacturer_id == 0x00FA
    assert p.sv.unit == 32 and p.sv.sources[0].params == {"value": 23.5}
    t = load_profile("temperature")
    assert t.device_type == 0xFA02 and t.pv.unit == 32 and t.range == (0, 150)
    assert t.pv.sources[0].type == "ramp"


def test_units():
    assert units.code("kPa") == 12 and units.name(12) == "kPa"
    assert units.code("%") == 57 and units.UNKNOWN == 250
    with pytest.raises(ValueError):
        units.code("furlong")
    with pytest.raises(ValueError):
        units.name(999)


def test_override_tag():
    assert load_profile("pressure", {"tag": "PT-999"}).tag == "PT-999"


def test_source_list(tmp_path):
    f = tmp_path / "p.yaml"
    text = _pressure_text()
    f.write_text(text.replace(
        "source: {type: sine, offset: 400, amplitude: 50, period: 30}",
        "source: [{type: sine, offset: 400, amplitude: 50, period: 30}, {type: noise, sigma: 1}]"))
    assert len(load_profile(f).pv.sources) == 2


def test_rejects_unknown_unit(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text(_pressure_text()
                 .replace("unit: kPa", "unit: furlong"))
    with pytest.raises(ProfileError, match="unit"):
        load_profile(f)


def test_rejects_range_outside_sensor_limits():
    with pytest.raises(ProfileError, match="range: must lie within sensor_limits"):
        load_profile("pressure", {"range": [0, 5000]})


def test_overrides_applied():
    p = load_profile("pressure", {"range": [0, 600], "damping": 2.0,
                                  "sensor_limits": [-50, 1000]})
    assert p.range == (0, 600) and p.damping == 2.0 and p.sensor_limits == (-50, 1000)


def test_manufacturer_id_16_bit():
    assert load_profile("pressure", {"manufacturer_id": 0x1234}).manufacturer_id == 0x1234


def test_rejects_bad_range_and_values():
    for ov, key in [({"range": [10, 10]}, "range: LRV must be below URV"),
                    ({"range": [float("nan"), 5]}, r"range\[0\]: must be finite"),
                    ({"damping": -1}, "damping: must be >= 0"),
                    ({"alarm": "mid"}, "alarm: must be"),
                    ({"min_span": 0}, "min_span: must be > 0")]:
        with pytest.raises(ProfileError, match=key):
            load_profile("pressure", ov)


def test_rejects_tag_longer_than_8():
    with pytest.raises(ProfileError, match="tag"):
        load_profile("pressure", {"tag": "PT-1234567"})


def test_rejects_bad_text():
    with pytest.raises(ProfileError, match="descriptor"):
        load_profile("pressure", {"descriptor": "lower~"})
    with pytest.raises(ProfileError, match="long_tag"):
        load_profile("pressure", {"long_tag": "x" * 33})
    with pytest.raises(ProfileError, match="long_tag"):
        load_profile("pressure", {"long_tag": "€"})


def test_rejects_unknown_key():
    with pytest.raises(ProfileError, match="dampng"):
        load_profile("pressure", {"dampng": 1.0})


def test_rejects_bad_source(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text(_pressure_text()
                 .replace("amplitude: 50", "amplitud: 50"))
    with pytest.raises(ProfileError, match="amplitud"):
        load_profile(f)


def test_unknown_profile_name():
    with pytest.raises(ProfileError):
        load_profile("nope")


def test_readme_profile_example_loads(tmp_path):
    import re
    from pathlib import Path
    from hart_sim.profile import load_profile
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme.split("\n## Profiles\n", 1)[1].split("\n## ", 1)[0]
    block = re.search(r"```yaml\n(.*?)```", section, re.S).group(1)
    path = tmp_path / "readme.yaml"
    path.write_text(block, encoding="utf-8")
    assert load_profile(str(path)).tag == "PT-101"
