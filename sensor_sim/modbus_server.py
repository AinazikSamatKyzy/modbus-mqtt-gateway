"""A minimal Modbus TCP server that simulates an industrial temperature sensor.

Written from scratch (no Modbus library) to show how the protocol works:

    TCP frame = MBAP header (7 bytes) + PDU
    MBAP      = transaction id (2) | protocol id (2, always 0) | length (2) | unit id (1)
    PDU       = function code (1) | data (n)

All multi-byte fields are big-endian. Supported function codes:
    0x01 Read Coils, 0x03 Read Holding Registers,
    0x05 Write Single Coil, 0x06 Write Single Register
"""

import asyncio
import logging
import math
import os
import random
import struct

from common import registers as reg

log = logging.getLogger("sensor_sim")

# Function codes
READ_COILS = 0x01
READ_HOLDING_REGISTERS = 0x03
WRITE_SINGLE_COIL = 0x05
WRITE_SINGLE_REGISTER = 0x06

# Exception codes
ILLEGAL_FUNCTION = 0x01
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03

MBAP = struct.Struct(">HHHB")  # transaction id, protocol id, length, unit id


class DataStore:
    """The device's memory: holding registers and coils."""

    def __init__(self, n_registers: int, n_coils: int):
        self.holding = [0] * n_registers
        self.coils = [False] * n_coils


def exception_response(function_code: int, code: int) -> bytes:
    # An error reply sets the top bit of the function code (e.g. 0x03 -> 0x83)
    return bytes([function_code | 0x80, code])


def handle_pdu(store: DataStore, pdu: bytes) -> bytes:
    """Process one request PDU and return the response PDU."""
    if not pdu:
        return exception_response(0, ILLEGAL_FUNCTION)
    fc = pdu[0]

    if fc in (READ_COILS, READ_HOLDING_REGISTERS):
        if len(pdu) != 5:
            return exception_response(fc, ILLEGAL_DATA_VALUE)
        start, count = struct.unpack(">HH", pdu[1:5])
        table = store.coils if fc == READ_COILS else store.holding
        max_count = 2000 if fc == READ_COILS else 125  # limits from the Modbus spec
        if not 1 <= count <= max_count:
            return exception_response(fc, ILLEGAL_DATA_VALUE)
        if start + count > len(table):
            return exception_response(fc, ILLEGAL_DATA_ADDRESS)

        values = table[start:start + count]
        if fc == READ_HOLDING_REGISTERS:
            payload = struct.pack(f">{count}H", *values)
        else:
            # Coils are packed 8 per byte, first coil in the lowest bit
            payload = bytearray((count + 7) // 8)
            for i, on in enumerate(values):
                if on:
                    payload[i // 8] |= 1 << (i % 8)
            payload = bytes(payload)
        return bytes([fc, len(payload)]) + payload

    if fc in (WRITE_SINGLE_COIL, WRITE_SINGLE_REGISTER):
        if len(pdu) != 5:
            return exception_response(fc, ILLEGAL_DATA_VALUE)
        address, value = struct.unpack(">HH", pdu[1:5])
        table = store.coils if fc == WRITE_SINGLE_COIL else store.holding
        if address >= len(table):
            return exception_response(fc, ILLEGAL_DATA_ADDRESS)
        if fc == WRITE_SINGLE_COIL:
            if value not in (0xFF00, 0x0000):  # the spec only allows ON=0xFF00, OFF=0x0000
                return exception_response(fc, ILLEGAL_DATA_VALUE)
            store.coils[address] = value == 0xFF00
        else:
            store.holding[address] = value
        return pdu  # a successful write echoes the request

    return exception_response(fc, ILLEGAL_FUNCTION)


async def handle_client(store: DataStore, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    log.info("client connected: %s", peer)
    try:
        while True:
            header = await reader.readexactly(MBAP.size)
            transaction_id, protocol_id, length, unit_id = MBAP.unpack(header)
            if protocol_id != 0 or not 2 <= length <= 254:
                log.warning("bad MBAP header from %s, closing", peer)
                break
            pdu = await reader.readexactly(length - 1)  # length counts the unit id byte too
            response = handle_pdu(store, pdu)
            writer.write(MBAP.pack(transaction_id, 0, len(response) + 1, unit_id) + response)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        log.info("client disconnected: %s", peer)
        writer.close()


class SensorModel:
    """Fake physics: the room heats up when the fan is off and cools when it is on."""

    HOT_TARGET = 36.0   # where temperature drifts with the fan off
    COOL_TARGET = 22.0  # where temperature drifts with the fan on
    RATE = 0.04         # fraction of the gap closed per step

    def __init__(self, start_temp: float = 25.0, seed: int | None = None):
        self.temperature = start_temp
        self.step_count = 0
        self.rng = random.Random(seed)

    def step(self, fan_on: bool) -> dict:
        target = self.COOL_TARGET if fan_on else self.HOT_TARGET
        self.temperature += (target - self.temperature) * self.RATE + self.rng.gauss(0, 0.05)
        self.step_count += 1
        humidity = 45 + 5 * math.sin(self.step_count / 30) + self.rng.gauss(0, 0.2)
        pressure = 1013.2 + 0.8 * math.sin(self.step_count / 120)
        return {"temperature": self.temperature, "humidity": humidity, "pressure": pressure}

    def write_to(self, store: DataStore, values: dict, fan_on: bool) -> None:
        store.holding[reg.HR_TEMPERATURE] = reg.encode_scaled(values["temperature"], signed=True)
        store.holding[reg.HR_HUMIDITY] = reg.encode_scaled(min(max(values["humidity"], 0), 100))
        store.holding[reg.HR_PRESSURE] = reg.encode_scaled(values["pressure"])
        status = reg.STATUS_RUNNING | (reg.STATUS_FAN_ON if fan_on else 0)
        store.holding[reg.HR_STATUS] = status
        store.holding[reg.HR_HEARTBEAT] = self.step_count & 0xFFFF


async def simulate(store: DataStore, period_s: float):
    model = SensorModel()
    while True:
        fan_on = store.coils[reg.COIL_FAN]
        model.write_to(store, model.step(fan_on), fan_on)
        await asyncio.sleep(period_s)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    host = os.getenv("MODBUS_HOST", "0.0.0.0")
    port = int(os.getenv("MODBUS_PORT", "5020"))  # 502 is standard but needs admin rights
    period = float(os.getenv("SIM_PERIOD_S", "0.5"))

    store = DataStore(reg.HR_COUNT, reg.COIL_COUNT)
    server = await asyncio.start_server(lambda r, w: handle_client(store, r, w), host, port)
    log.info("Modbus TCP sensor simulator listening on %s:%d", host, port)
    async with server:
        await asyncio.gather(server.serve_forever(), simulate(store, period))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
