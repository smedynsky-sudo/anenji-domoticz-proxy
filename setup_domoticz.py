#!/usr/bin/env python3
import json
import sys
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
HARDWARE = "ANENJI Inverter"

SENSORS = [
    ("ANENJI · PV мощность", 1004, "W", "pv_power"),
    ("ANENJI · Нагрузка", 1004, "W", "output_active_power"),
    ("ANENJI · Мощность сети", 1004, "W", "grid_power"),
    ("ANENJI · Мощность АКБ", 1004, "W", "battery_power"),
    ("ANENJI · Заряд АКБ", 2, "", "battery_soc"),
    ("ANENJI · Нагрузка %", 2, "", "load_percent"),
    ("ANENJI · Напряжение сети", 1004, "V", "grid_voltage"),
    ("ANENJI · Выходное напряжение", 1004, "V", "output_voltage"),
    ("ANENJI · Напряжение АКБ", 1004, "V", "battery_voltage"),
    ("ANENJI · Напряжение PV", 1004, "V", "pv_voltage"),
    ("ANENJI · Ток PV", 1004, "A", "pv_current"),
    ("ANENJI · Выходной ток", 1004, "A", "output_current"),
    ("ANENJI · Ток АКБ", 1004, "A", "battery_signed_current"),
    ("ANENJI · Частота сети", 1004, "Hz", "grid_frequency"),
    ("ANENJI · Выходная частота", 1004, "Hz", "output_frequency"),
    ("ANENJI · Температура инвертора", 80, "", "inverter_temperature"),
    ("ANENJI · Температура DC/DC", 80, "", "dcdc_temperature"),
    ("ANENJI · Режим", 7, "", "mode"),
    ("ANENJI · Состояние", 7, "", "online"),
]


def api(**params):
    url = BASE + "/json.htm?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


hardware = api(type="command", param="gethardware").get("result", [])
match = next((h for h in hardware if h.get("Name") == HARDWARE), None)
if match:
    hwidx = int(match["idx"])
else:
    result = api(type="command", param="addhardware", htype=15, port=1, name=HARDWARE, enabled="true")
    if result.get("status") != "OK":
        raise SystemExit(f"hardware creation failed: {result}")
    hwidx = int(result["idx"])

devices = api(type="command", param="getdevices", filter="all", used="all", order="Name").get("result", [])
by_name = {d.get("Name"): d for d in devices if int(d.get("HardwareID", -1)) == hwidx}
mapping = {}
for name, sensor_type, unit, key in SENSORS:
    device = by_name.get(name)
    if device and sensor_type == 1004 and device.get("SubType") != "Custom Sensor":
        result = api(type="command", param="deletedevice", idx=int(device["idx"]))
        if result.get("status") != "OK":
            raise SystemExit(f"wrong device deletion failed for {name}: {result}")
        device = None
    if device and sensor_type == 7 and device.get("SubType") != "Alert":
        result = api(type="command", param="deletedevice", idx=int(device["idx"]))
        if result.get("status") != "OK":
            raise SystemExit(f"wrong alert device deletion failed for {name}: {result}")
        device = None
    if not device:
        params = dict(type="command", param="createvirtualsensor", idx=hwidx, sensorname=name, sensortype=sensor_type)
        if unit:
            params["sensoroptions"] = f"1;{unit}"
        result = api(**params)
        if result.get("status") != "OK":
            raise SystemExit(f"device creation failed for {name}: {result}")
        device_idx = int(result["idx"])
    else:
        device_idx = int(device["idx"])
    mapping[key] = device_idx

print(json.dumps({"url": BASE, "hardware_idx": hwidx, "devices": mapping}, ensure_ascii=False, indent=2))

