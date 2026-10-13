"""IoT gateway: polls a Modbus TCP device and publishes the data to MQTT.

    sensor (Modbus TCP) --poll--> gateway --publish--> MQTT broker --> subscribers
                       <--write coil--      <--command--

Topics (BASE = demo/line1/sensor01):
    BASE/telemetry   JSON measurements, every poll
    BASE/alarm       JSON, only when a high-temperature alarm starts or clears (retained)
    BASE/status      "online"/"offline" (retained, offline is sent by the broker as Last Will)
    BASE/fan/mode    current fan mode (retained)
    BASE/cmd/fan     send "on", "off" or "auto" here to control the fan
"""

import asyncio
import json
import logging
import os
import time

import paho.mqtt.client as mqtt
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from common import registers as reg
from gateway.logic import FanController, Hysteresis

log = logging.getLogger("gateway")


class Config:
    def __init__(self):
        self.modbus_host = os.getenv("MODBUS_HOST", "127.0.0.1")
        self.modbus_port = int(os.getenv("MODBUS_PORT", "5020"))
        self.device_id = int(os.getenv("MODBUS_DEVICE_ID", "1"))
        self.mqtt_host = os.getenv("MQTT_HOST", "127.0.0.1")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.base_topic = os.getenv("BASE_TOPIC", "demo/line1/sensor01")
        self.poll_s = float(os.getenv("POLL_S", "1.0"))
        self.fan_on_at = float(os.getenv("FAN_ON_AT", "30"))
        self.fan_off_at = float(os.getenv("FAN_OFF_AT", "27"))
        self.alarm_on_at = float(os.getenv("ALARM_ON_AT", "33"))
        self.alarm_off_at = float(os.getenv("ALARM_OFF_AT", "31"))


class Gateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.fan = FanController(cfg.fan_on_at, cfg.fan_off_at)
        self.alarm = Hysteresis(cfg.alarm_on_at, cfg.alarm_off_at)
        self.modbus: AsyncModbusTcpClient | None = None  # must be created inside the event loop
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="modbus-gateway")

    def topic(self, suffix: str) -> str:
        return f"{self.cfg.base_topic}/{suffix}"

    # ---------- MQTT ----------
    def start_mqtt(self):
        # Last Will: if the gateway dies, the broker publishes "offline" for us
        self.mqtt.will_set(self.topic("status"), "offline", qos=1, retain=True)
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=30)
        self.mqtt.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=30)
        self.mqtt.loop_start()  # paho runs its network loop in a background thread

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected to %s:%d", self.cfg.mqtt_host, self.cfg.mqtt_port)
        client.subscribe(self.topic("cmd/fan"), qos=1)
        client.publish(self.topic("status"), "online", qos=1, retain=True)
        client.publish(self.topic("fan/mode"), self.fan.mode, qos=1, retain=True)

    def on_message(self, client, userdata, msg):
        # Called from paho's thread: only update simple state here
        command = msg.payload.decode(errors="replace")
        try:
            self.fan.set_mode(command)
        except ValueError as e:
            log.warning("ignored command on %s: %s", msg.topic, e)
            return
        log.info("fan mode -> %s", self.fan.mode)
        client.publish(self.topic("fan/mode"), self.fan.mode, qos=1, retain=True)

    def publish_json(self, suffix: str, data: dict, retain: bool = False):
        self.mqtt.publish(self.topic(suffix), json.dumps(data), qos=1, retain=retain)

    # ---------- Modbus ----------
    async def ensure_modbus(self) -> bool:
        if self.modbus.connected:
            return True
        ok = await self.modbus.connect()
        if ok:
            log.info("Modbus connected to %s:%d", self.cfg.modbus_host, self.cfg.modbus_port)
        return ok

    async def poll_once(self) -> dict:
        rr = await self.modbus.read_holding_registers(reg.HR_TEMPERATURE, count=reg.HR_COUNT,
                                                      device_id=self.cfg.device_id)
        if rr.isError():
            raise ModbusException(f"device returned error: {rr}")
        data = reg.decode(rr.registers)

        want_fan = self.fan.desired(data["temperature_c"])
        if want_fan != data["fan_on"]:
            wr = await self.modbus.write_coil(reg.COIL_FAN, want_fan, device_id=self.cfg.device_id)
            if wr.isError():
                raise ModbusException(f"fan write failed: {wr}")
            log.info("fan %s (mode=%s, temp=%.1f)", "ON" if want_fan else "OFF",
                     self.fan.mode, data["temperature_c"])
        return data

    # ---------- main loop ----------
    async def run(self):
        self.modbus = AsyncModbusTcpClient(self.cfg.modbus_host, port=self.cfg.modbus_port,
                                           timeout=2, retries=1)
        self.start_mqtt()
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                if not await self.ensure_modbus():
                    raise ConnectionError("cannot reach Modbus device")
                data = await self.poll_once()
                backoff = 1.0
            except (ModbusException, ConnectionError, OSError) as e:
                log.warning("Modbus problem: %s (retry in %.0fs)", e, backoff)
                self.publish_json("telemetry", {"ts": time.time(), "error": str(e)})
                self.modbus.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)  # exponential backoff
                continue

            data["ts"] = round(time.time(), 3)
            data["fan_mode"] = self.fan.mode
            self.publish_json("telemetry", data)

            if self.alarm.update(data["temperature_c"]):
                self.publish_json("alarm", {
                    "ts": data["ts"], "type": "HIGH_TEMPERATURE", "active": self.alarm.active,
                    "temperature_c": data["temperature_c"], "threshold_c": self.cfg.alarm_on_at,
                }, retain=True)
                log.warning("ALARM %s at %.1f C", "RAISED" if self.alarm.active else "CLEARED",
                            data["temperature_c"])

            await asyncio.sleep(max(0.0, self.cfg.poll_s - (time.monotonic() - started)))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)  # we log connection problems ourselves
    gw = Gateway(Config())
    try:
        asyncio.run(gw.run())
    except KeyboardInterrupt:
        pass
    finally:
        gw.mqtt.publish(gw.topic("status"), "offline", qos=1, retain=True).wait_for_publish(2)
        gw.mqtt.loop_stop()
        gw.mqtt.disconnect()


if __name__ == "__main__":
    main()
