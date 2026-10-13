"""Register map shared by the sensor simulator and the gateway.

Modbus registers are 16-bit unsigned words, so real-world values are
stored as scaled integers (e.g. 23.4 C -> 234) and decoded on the other side.
"""

# Holding registers (function code 3 to read, 6 to write)
HR_TEMPERATURE = 0   # deg C x10, signed int16
HR_HUMIDITY = 1      # %RH x10
HR_PRESSURE = 2      # hPa x10
HR_STATUS = 3        # bit0 = running, bit1 = fan on
HR_HEARTBEAT = 4     # increments every update, wraps at 65535
HR_COUNT = 5

# Coils (function code 1 to read, 5 to write)
COIL_FAN = 0
COIL_COUNT = 1

SCALE = 10

STATUS_RUNNING = 1 << 0
STATUS_FAN_ON = 1 << 1


def to_int16(word: int) -> int:
    """Interpret an unsigned 16-bit word as a signed value (two's complement)."""
    return word - 0x10000 if word & 0x8000 else word


def from_int16(value: int) -> int:
    """Store a signed value in an unsigned 16-bit word (two's complement)."""
    return value & 0xFFFF


def encode_scaled(value: float, signed: bool = False) -> int:
    raw = round(value * SCALE)
    if signed:
        if not -0x8000 <= raw <= 0x7FFF:
            raise ValueError(f"{value} does not fit in a signed 16-bit register")
        return from_int16(raw)
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f"{value} does not fit in an unsigned 16-bit register")
    return raw


def decode(registers: list[int]) -> dict:
    """Turn the raw holding registers into engineering values."""
    if len(registers) < HR_COUNT:
        raise ValueError(f"expected {HR_COUNT} registers, got {len(registers)}")
    status = registers[HR_STATUS]
    return {
        "temperature_c": to_int16(registers[HR_TEMPERATURE]) / SCALE,
        "humidity_pct": registers[HR_HUMIDITY] / SCALE,
        "pressure_hpa": registers[HR_PRESSURE] / SCALE,
        "running": bool(status & STATUS_RUNNING),
        "fan_on": bool(status & STATUS_FAN_ON),
        "heartbeat": registers[HR_HEARTBEAT],
    }
