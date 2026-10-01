#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import signal
import socket
import struct
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
PROXY_PORT = int(os.getenv("PROXY_PORT", "18899"))
UPSTREAM_HOST = os.getenv("UPSTREAM_HOST", "8.218.202.213")
UPSTREAM_PORT = int(os.getenv("UPSTREAM_PORT", "18899"))
LOCAL_ONLY = os.getenv("LOCAL_ONLY", "1") == "1"
HTTP_PORT = int(os.getenv("HTTP_PORT", "8088"))
HTTP_ENABLED = os.getenv("HTTP_ENABLED", "0") == "1"
DNS_HOST = os.getenv("DNS_HOST", "0.0.0.0")
DNS_PORT = int(os.getenv("DNS_PORT", "53"))
DNS_UPSTREAM = os.getenv("DNS_UPSTREAM", "1.1.1.1")
DNS_OVERRIDE = os.getenv("DNS_OVERRIDE", "dtu_ess.eybond.com").rstrip(".").lower()
DNS_OVERRIDE_IP = os.getenv("DNS_OVERRIDE_IP", "127.0.0.1")
DTU_IP = os.getenv("DTU_IP", "192.168.1.100")
ADVERTISE_IP = os.getenv("ADVERTISE_IP", "127.0.0.1")
WEB_DIR = Path(os.getenv("WEB_DIR", Path(__file__).with_name("web")))
DOMOTICZ_CONFIG = Path(os.getenv("DOMOTICZ_CONFIG", Path(__file__).with_name("domoticz.json")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smartess-proxy")

state = {
    "online": False,
    "updated_at": None,
    "logger": {},
    "values": {},
    "raw_registers": {},
    "connection": {"upstream": f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"},
}
history = deque(maxlen=1440)
event_clients = set()
pending_reads = deque()
active_sessions = 0
last_domoticz_push = 0.0
domoticz_push_task = None
domoticz_last_state = {}

try:
    domoticz = json.loads(DOMOTICZ_CONFIG.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    domoticz = {}


def dns_question_name(data):
    labels, offset = [], 12
    while offset < len(data):
        size = data[offset]
        offset += 1
        if size == 0:
            break
        labels.append(data[offset:offset + size].decode("ascii", "ignore"))
        offset += size
    return ".".join(labels).lower(), offset + 4


class DNSProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        asyncio.create_task(self.answer(data, addr))

    async def answer(self, data, addr):
        try:
            name, question_end = dns_question_name(data)
            qtype = struct.unpack("!H", data[question_end - 4:question_end - 2])[0]
            if name == DNS_OVERRIDE and qtype == 1:
                flags = b"\x81\x80"
                header = data[:2] + flags + data[4:6] + b"\x00\x01\x00\x00\x00\x00"
                answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + socket.inet_aton(DNS_OVERRIDE_IP)
                self.transport.sendto(header + data[12:question_end] + answer, addr)
                return
            loop = asyncio.get_running_loop()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            try:
                await loop.sock_sendto(sock, data, (DNS_UPSTREAM, 53))
                response = await asyncio.wait_for(loop.sock_recv(sock, 4096), 3)
                self.transport.sendto(response, addr)
            finally:
                sock.close()
        except Exception as exc:
            log.debug("DNS request failed: %s", exc)

REGISTER_MAP = {
    201: ("mode", 1, "enum"),
    202: ("grid_voltage", 0.1, "V"),
    203: ("grid_frequency", 0.01, "Hz"),
    204: ("grid_power", 1, "W"),
    205: ("inverter_voltage", 0.1, "V"),
    206: ("inverter_current", 0.1, "A"),
    207: ("inverter_frequency", 0.01, "Hz"),
    208: ("inverter_power", 1, "W"),
    209: ("charging_power", 1, "W"),
    210: ("output_voltage", 0.1, "V"),
    211: ("output_current", 0.1, "A"),
    212: ("output_frequency", 0.01, "Hz"),
    213: ("output_active_power", 1, "W"),
    214: ("output_apparent_power", 1, "VA"),
    215: ("battery_voltage", 0.1, "V"),
    216: ("battery_current", 0.1, "A"),
    217: ("battery_power", 1, "W"),
    219: ("pv_voltage", 0.1, "V"),
    220: ("pv_current", 0.1, "A"),
    223: ("pv_power", 1, "W"),
    224: ("pv_charge_power", 1, "W"),
    225: ("load_percent", 1, "%"),
    226: ("dcdc_temperature", 1, "°C"),
    227: ("inverter_temperature", 1, "°C"),
    229: ("battery_soc", 1, "%"),
    232: ("battery_signed_current", 0.1, "A"),
}

MODES = {0: "Запуск", 1: "Ожидание", 2: "Сеть", 3: "Автономно", 4: "Байпас", 5: "Зарядка", 6: "Авария"}
SIGNED_REGISTERS = {204, 208, 209, 216, 217, 232}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


async def publish():
    global last_domoticz_push, domoticz_push_task
    payload = f"data: {json.dumps(state, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()
    dead = []
    for writer in event_clients:
        try:
            writer.write(payload)
            await writer.drain()
        except Exception:
            dead.append(writer)
    for writer in dead:
        event_clients.discard(writer)
    if domoticz and time.monotonic() - last_domoticz_push >= 5 and (
        domoticz_push_task is None or domoticz_push_task.done()
    ):
        last_domoticz_push = time.monotonic()
        domoticz_push_task = asyncio.create_task(asyncio.to_thread(push_domoticz_sync))


def push_domoticz_sync():
    configured_urls = domoticz.get("urls") or [domoticz.get("url", "")]
    bases = [str(url).rstrip("/") for url in configured_urls if str(url).strip()]
    devices = domoticz.get("devices", {})
    if not bases or not devices:
        return
    updates = []
    for key, idx in devices.items():
        if key == "online":
            updates.append((key, idx, 1 if state["online"] else 4, "Онлайн" if state["online"] else "Нет соединения"))
            continue
        if key == "mode":
            mode = state["values"].get("mode", {}).get("label", "Неизвестно")
            grid_voltage = float(state["values"].get("grid_voltage", {}).get("value", 0) or 0)
            grid_ok = grid_voltage >= 180
            text = f"{mode} · сеть {grid_voltage:g} V" if grid_ok else f"{mode} · СЕТЬ ОТСУТСТВУЕТ"
            updates.append((key, idx, 1 if grid_ok else 4, text))
            continue
        item = state["values"].get(key)
        if not item:
            continue
        value = item.get("label", item.get("value")) if key == "mode" else item.get("value")
        updates.append((key, idx, 0, value))
    for base in bases:
        for key, idx, nvalue, svalue in updates:
            # Alert text may contain live voltage, but Domoticz notifications
            # should only see an update when the actual alert state changes.
            fingerprint = nvalue if key in ("mode", "online") else (nvalue, str(svalue))
            state_key = (base, key)
            if key in ("mode", "online") and domoticz_last_state.get(state_key) == fingerprint:
                continue
            query = urllib.parse.urlencode({
                "type": "command", "param": "udevice", "idx": idx,
                "nvalue": nvalue, "svalue": str(svalue),
            })
            try:
                with urllib.request.urlopen(f"{base}/json.htm?{query}", timeout=3) as response:
                    result = json.load(response)
                    if result.get("status") != "OK":
                        log.warning("Domoticz %s rejected idx %s: %s", base, idx, result)
                    elif key in ("mode", "online"):
                        domoticz_last_state[state_key] = fingerprint
            except Exception as exc:
                log.warning("Domoticz %s update idx %s failed: %s", base, idx, exc)


def decode_value(register, raw):
    if register in SIGNED_REGISTERS and raw >= 0x8000:
        raw -= 0x10000
    name, scale, unit = REGISTER_MAP[register]
    value = raw * scale
    if scale < 1:
        value = round(value, 2)
    return name, value, unit


def update_registers(start, values):
    changed = False
    for offset, raw in enumerate(values):
        register = start + offset
        state["raw_registers"][str(register)] = raw
        if register in REGISTER_MAP:
            name, value, unit = decode_value(register, raw)
            if name == "mode":
                state["values"][name] = {"value": value, "label": MODES.get(value, f"Режим {value}"), "unit": unit}
            else:
                state["values"][name] = {"value": value, "unit": unit}
            changed = True
    if changed:
        state["updated_at"] = now_iso()
        snapshot = {"time": state["updated_at"]}
        for key in ("pv_power", "grid_power", "output_active_power", "battery_voltage", "battery_soc", "load_percent"):
            if key in state["values"]:
                snapshot[key] = state["values"][key]["value"]
        history.append(snapshot)
    return changed


class ProtocolStream:
    def __init__(self, from_server):
        self.buf = bytearray()
        self.from_server = from_server

    def feed(self, data):
        self.buf.extend(data)
        frames = []
        while self.buf:
            if self.buf.startswith(b"AT+"):
                end = self.buf.find(b"\r\n")
                if end < 0:
                    break
                frames.append(bytes(self.buf[: end + 2]))
                del self.buf[: end + 2]
                continue
            if self.buf[0] != 1:
                del self.buf[0]
                continue
            if len(self.buf) < 3:
                break
            function = self.buf[1]
            if self.from_server:
                length = 8 if function in (3, 4, 6, 16) else None
            elif function in (3, 4):
                length = self.buf[2] + 5
            elif function & 0x80:
                length = 5
            else:
                length = 8
            if not length or len(self.buf) < length:
                break
            frames.append(bytes(self.buf[:length]))
            del self.buf[:length]
        return frames


async def inspect_frame(frame, from_server):
    if frame.startswith(b"AT+"):
        text = frame.decode("ascii", "replace").strip()
        if not from_server and ":" in text:
            key, value = text[3:].split(":", 1)
            state["logger"][key.lower()] = value
            state["updated_at"] = now_iso()
            await publish()
        return
    if len(frame) < 5:
        return
    function = frame[1]
    if from_server and function in (3, 4) and len(frame) == 8:
        start, count = struct.unpack(">HH", frame[2:6])
        pending_reads.append((start, count, time.monotonic()))
        while len(pending_reads) > 30:
            pending_reads.popleft()
    elif not from_server and function in (3, 4) and pending_reads:
        start, count, _ = pending_reads.popleft()
        byte_count = frame[2]
        values = [struct.unpack(">H", frame[i:i + 2])[0] for i in range(3, 3 + byte_count, 2)]
        if update_registers(start, values[:count]):
            await publish()


async def pipe(reader, writer, stream):
    try:
        while data := await reader.read(4096):
            writer.write(data)
            await writer.drain()
            for frame in stream.feed(data):
                await inspect_frame(frame, stream.from_server)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def modbus_crc(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def read_holding(start, count):
    payload = struct.pack(">BBHH", 1, 3, start, count)
    return payload + struct.pack("<H", modbus_crc(payload))


async def local_poll(reader, writer):
    """Poll one range at a time and bind each response to that exact range."""
    await asyncio.sleep(0.5)
    while not writer.is_closing():
        for start, count in ((200, 22), (223, 13)):
            writer.write(read_holding(start, count))
            await writer.drain()
            try:
                header = await asyncio.wait_for(reader.readexactly(3), 3)
                if header[:2] != b"\x01\x03":
                    raise ValueError(f"unexpected Modbus header {header.hex()}")
                byte_count = header[2]
                body_crc = await asyncio.wait_for(reader.readexactly(byte_count + 2), 3)
                body = body_crc[:byte_count]
                values = [
                    struct.unpack(">H", body[i:i + 2])[0]
                    for i in range(0, len(body), 2)
                ]
                if update_registers(start, values[:count]):
                    await publish()
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
                log.warning("Local Modbus read %d/%d failed: %s", start, count, exc)
                raise
            await asyncio.sleep(0.25)
        await asyncio.sleep(3)


async def callback_announcer():
    """Ask the EyeBond collector to reconnect whenever its TCP session is absent."""
    loop = asyncio.get_running_loop()
    message = f"set>server={ADVERTISE_IP}:{PROXY_PORT};".encode("ascii")
    while True:
        if active_sessions == 0:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            try:
                await loop.sock_sendto(sock, message, (DTU_IP, 58899))
                log.info("Callback requested from %s to %s:%d", DTU_IP, ADVERTISE_IP, PROXY_PORT)
            except OSError as exc:
                log.warning("Callback request failed: %s", exc)
            finally:
                sock.close()
        await asyncio.sleep(10)


async def receive_local(reader):
    stream = ProtocolStream(False)
    while data := await reader.read(4096):
        for frame in stream.feed(data):
            await inspect_frame(frame, False)


async def handle_proxy(reader, writer):
    global active_sessions
    peer = writer.get_extra_info("peername")
    log.info("DTU connected from %s", peer)
    active_sessions += 1
    state["online"] = True
    state["connection"]["client"] = peer[0] if peer else "unknown"
    state["updated_at"] = now_iso()
    pending_reads.clear()
    await publish()
    try:
        if LOCAL_ONLY:
            await local_poll(reader, writer)
        else:
            upstream_reader, upstream_writer = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
            await asyncio.gather(
                pipe(reader, upstream_writer, ProtocolStream(False)),
                pipe(upstream_reader, writer, ProtocolStream(True)),
                local_poll(reader, writer),
            )
    except Exception as exc:
        log.warning("Proxy session ended: %s", exc)
    finally:
        active_sessions = max(0, active_sessions - 1)
        state["online"] = active_sessions > 0
        state["updated_at"] = now_iso()
        await publish()
        writer.close()


CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}


async def http_response(writer, status, body, content_type="application/json; charset=utf-8", extra=""):
    if isinstance(body, str):
        body = body.encode()
    headers = f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n{extra}\r\n"
    writer.write(headers.encode() + body)
    await writer.drain()
    writer.close()


async def handle_http(reader, writer):
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ")
        path = urlparse(target).path
        if method != "GET":
            return await http_response(writer, "405 Method Not Allowed", "{}")
        if path == "/api/state":
            return await http_response(writer, "200 OK", json.dumps(state, ensure_ascii=False))
        if path == "/api/history":
            return await http_response(writer, "200 OK", json.dumps(list(history), ensure_ascii=False))
        if path == "/events":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
            await writer.drain()
            event_clients.add(writer)
            writer.write(f"data: {json.dumps(state, ensure_ascii=False)}\n\n".encode())
            await writer.drain()
            try:
                while not reader.at_eof():
                    await asyncio.sleep(15)
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
            finally:
                event_clients.discard(writer)
            return
        requested = "index.html" if path == "/" else path.lstrip("/")
        file = (WEB_DIR / requested).resolve()
        if WEB_DIR.resolve() not in file.parents and file != WEB_DIR.resolve():
            return await http_response(writer, "403 Forbidden", "Forbidden", "text/plain")
        if not file.is_file():
            return await http_response(writer, "404 Not Found", "Not found", "text/plain")
        return await http_response(writer, "200 OK", file.read_bytes(), CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
    except Exception as exc:
        log.debug("HTTP client ended: %s", exc)
        try:
            writer.close()
        except Exception:
            pass


async def main():
    proxy = await asyncio.start_server(handle_proxy, LISTEN_HOST, PROXY_PORT)
    http = await asyncio.start_server(handle_http, LISTEN_HOST, HTTP_PORT) if HTTP_ENABLED else None
    loop = asyncio.get_running_loop()
    dns_transport, _ = await loop.create_datagram_endpoint(DNSProtocol, local_addr=(DNS_HOST, DNS_PORT))
    log.info("Proxy listening on %s:%d -> %s:%d", LISTEN_HOST, PROXY_PORT, UPSTREAM_HOST, UPSTREAM_PORT)
    if http:
        log.info("Dashboard listening on http://%s:%d", LISTEN_HOST, HTTP_PORT)
    else:
        log.info("Dashboard disabled")
    log.info("DNS listening on %s:%d, %s -> %s", DNS_HOST, DNS_PORT, DNS_OVERRIDE, DNS_OVERRIDE_IP)
    stop = asyncio.Event()
    announcer = asyncio.create_task(callback_announcer())
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    async with proxy:
        if http:
            async with http:
                await stop.wait()
        else:
            await stop.wait()
    announcer.cancel()
    await asyncio.gather(announcer, return_exceptions=True)
    dns_transport.close()


if __name__ == "__main__":
    asyncio.run(main())

