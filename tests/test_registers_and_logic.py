import pytest

from common import registers as reg
from gateway.logic import FanController, Hysteresis


def test_int16_roundtrip_negative_temperature():
    word = reg.encode_scaled(-5.3, signed=True)
    assert word == 0xFFCB  # -53 in two's complement
    assert reg.to_int16(word) == -53


def test_encode_rejects_out_of_range():
    with pytest.raises(ValueError):
        reg.encode_scaled(7000.0)          # 70000 > 65535
    with pytest.raises(ValueError):
        reg.encode_scaled(-0.1)            # unsigned can't be negative


def test_decode_registers():
    raw = [234, 451, 10132, reg.STATUS_RUNNING | reg.STATUS_FAN_ON, 7]
    d = reg.decode(raw)
    assert d == {"temperature_c": 23.4, "humidity_pct": 45.1, "pressure_hpa": 1013.2,
                 "running": True, "fan_on": True, "heartbeat": 7}


def test_hysteresis_does_not_flicker():
    h = Hysteresis(on_at=30, off_at=27)
    assert [h.update(v) for v in (29, 30, 29, 28, 27, 28)] == [False, True, False, False, True, False]
    assert h.active is False


def test_fan_manual_override_and_back_to_auto():
    fan = FanController(on_at=30, off_at=27)
    assert fan.desired(31) is True          # auto: hot -> on
    fan.set_mode("OFF")
    assert fan.desired(35) is False         # manual off wins
    fan.set_mode("auto")
    assert fan.desired(35) is True
    with pytest.raises(ValueError):
        fan.set_mode("turbo")
