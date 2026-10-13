import asyncio
import struct

from pymodbus.client import AsyncModbusTcpClient

from common import registers as reg
from sensor_sim.modbus_server import DataStore, SensorModel, handle_client, handle_pdu


def make_store():
    store = DataStore(reg.HR_COUNT, reg.COIL_COUNT)
    store.holding[:] = [234, 451, 10132, 1, 99]
    return store


# ---------- raw PDU bytes (exactly what goes on the wire) ----------

def test_read_holding_registers_bytes():
    # FC 03, start 0, count 2  ->  FC 03, 4 bytes, 0x00EA (234), 0x01C3 (451)
    assert handle_pdu(make_store(), bytes.fromhex("03 0000 0002")) == bytes.fromhex("03 04 00EA 01C3")


def test_write_coil_echoes_and_sets():
    store = make_store()
    request = bytes.fromhex("05 0000 FF00")
    assert handle_pdu(store, request) == request
    assert store.coils[0] is True


def test_exceptions():
    store = make_store()
    assert handle_pdu(store, bytes.fromhex("03 0004 0002")) == bytes.fromhex("83 02")  # past the end
    assert handle_pdu(store, bytes.fromhex("05 0000 1234")) == bytes.fromhex("85 03")  # invalid coil value
    assert handle_pdu(store, bytes.fromhex("2B")) == bytes.fromhex("AB 01")            # unsupported function


def test_read_coils_bit_packing():
    store = DataStore(1, 10)
    store.coils[0] = store.coils[3] = store.coils[9] = True
    # coils 0..7 -> 0b00001001, coils 8..9 -> 0b00000010
    assert handle_pdu(store, bytes.fromhex("01 0000 000A")) == bytes.fromhex("01 02 09 02")


def test_sensor_model_heats_and_cools():
    model = SensorModel(start_temp=25.0, seed=1)
    for _ in range(100):
        model.step(fan_on=False)
    hot = model.temperature
    for _ in range(100):
        model.step(fan_on=True)
    assert hot > 33 and model.temperature < hot - 8


# ---------- a real Modbus client (pymodbus) against our server ----------

def test_pymodbus_client_interoperates():
    async def scenario():
        store = make_store()
        server = await asyncio.start_server(lambda r, w: handle_client(store, r, w), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=2)
        try:
            assert await client.connect()
            rr = await client.read_holding_registers(0, count=reg.HR_COUNT, device_id=1)
            assert not rr.isError()
            assert reg.decode(rr.registers)["temperature_c"] == 23.4

            wr = await client.write_coil(reg.COIL_FAN, True, device_id=1)
            assert not wr.isError()
            assert store.coils[reg.COIL_FAN] is True

            bad = await client.read_holding_registers(100, count=1, device_id=1)
            assert bad.isError()  # our exception response is understood by pymodbus
        finally:
            client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_mbap_length_field():
    # length = unit id (1) + PDU; a read of 5 registers returns 2 + 10 = 12 PDU bytes -> length 13
    pdu = handle_pdu(make_store(), struct.pack(">BHH", 3, 0, 5))
    assert len(pdu) + 1 == 13
