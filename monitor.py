"""Subscribe to everything the gateway publishes and print it.

Usage:
    python monitor.py                 # watch the data
    python monitor.py fan off         # send a command, then keep watching
"""

import json
import os
import sys

import paho.mqtt.client as mqtt

HOST = os.getenv("MQTT_HOST", "127.0.0.1")
PORT = int(os.getenv("MQTT_PORT", "1883"))
BASE = os.getenv("BASE_TOPIC", "demo/line1/sensor01")


def on_connect(client, userdata, flags, reason_code, properties):
    client.subscribe(f"{BASE}/#")
    if len(sys.argv) == 3 and sys.argv[1] == "fan":
        client.publish(f"{BASE}/cmd/fan", sys.argv[2], qos=1)
        print(f">>> sent fan command: {sys.argv[2]}")


def on_message(client, userdata, msg):
    topic = msg.topic.removeprefix(BASE + "/")
    text = msg.payload.decode(errors="replace")
    if topic == "telemetry":
        d = json.loads(text)
        if "error" in d:
            print(f"[telemetry] ERROR: {d['error']}")
            return
        print(f"[telemetry] temp={d['temperature_c']:5.1f}C  hum={d['humidity_pct']:4.1f}%  "
              f"press={d['pressure_hpa']:.1f}hPa  fan={'ON ' if d['fan_on'] else 'OFF'} "
              f"({d['fan_mode']})  hb={d['heartbeat']}")
    elif topic == "alarm":
        d = json.loads(text)
        state = "RAISED" if d["active"] else "cleared"
        print(f"[ALARM] {d['type']} {state} at {d['temperature_c']}C")
    elif topic != "cmd/fan":
        print(f"[{topic}] {text}")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message
client.connect(HOST, PORT)
try:
    client.loop_forever()
except KeyboardInterrupt:
    pass
