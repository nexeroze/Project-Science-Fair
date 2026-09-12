"""BrainFlow Guard desktop application. Run from PowerShell with .\run.ps1."""
from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import sqlite3
import struct
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

try:
    from bleak import BleakClient, BleakScanner  # type: ignore
except ImportError:
    BleakClient = BleakScanner = None

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError:
    serial = None
    list_ports = None

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "brainflow_guard.sqlite3"
ENV_PATH = APP_DIR / ".env"
# A deliberately quiet, monochrome visual system. Status changes use contrast rather
# than bright colour, keeping the interface calm and easy to scan.
BG, CARD, CARD_2, TEXT, MUTED = "#0c0c0c", "#171717", "#252525", "#f2f2f2", "#969696"
TEAL, GREEN, AMBER, RED = "#e6e6e6", "#d9d9d9", "#ababab", "#707070"
# Shared with firmware/orthostat_esp32.ino — keep these identical on the wearable.
DEFAULT_SERVICE_UUID = "8f0a0001-7d3c-4e2a-9b1f-0c2d4e6f8001"
DEFAULT_TELEMETRY_UUID = "8f0a0002-7d3c-4e2a-9b1f-0c2d4e6f8001"
DEFAULT_COMMAND_UUID = "8f0a0003-7d3c-4e2a-9b1f-0c2d4e6f8001"
NORDIC_UART_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NORDIC_UART_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NORDIC_UART_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
ESP32_NAME_HINTS = ("orthostat", "brainflow", "esp32", "esp-32", "nimble", "uart")
STANDARD_BLE_SERVICES = {
    "00001800-0000-1000-8000-00805f9b34fb",
    "00001801-0000-1000-8000-00805f9b34fb",
    "0000180a-0000-1000-8000-00805f9b34fb",
    "0000180f-0000-1000-8000-00805f9b34fb",
}
POSTURE_CODES = {0: "LYING", 1: "SITTING", 2: "STANDING"}
HAPTIC_CODES = {"Low": 1, "Medium": 2, "High": 3}
TELEMETRY_ALIASES = {
    "ear_bvp": ("ear_bvp", "bvp", "ppg", "ear_ppg"),
    "bpm": ("bpm", "hr", "heart_rate"),
    "systolic": ("systolic", "sys", "sbp"),
    "diastolic": ("diastolic", "dia", "dbp"),
    "accel_g": ("accel_g", "accel", "g"),
    "posture": ("posture", "pose", "position"),
    "battery_ear": ("battery_ear", "batt_ear"),
    "battery_wrist": ("battery_wrist", "batt_wrist", "battery"),
    "battery_sleeve": ("battery_sleeve", "batt_sleeve"),
}


def load_dotenv() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Telemetry:
    ear_bvp: float = 98.0
    bpm: int = 70
    systolic: int = 120
    diastolic: int = 80
    accel_g: float = 1.0
    posture: str = "SITTING"
    battery_ear: int = 94
    battery_wrist: int = 98
    battery_sleeve: int = 89

    @property
    def drop_percent(self) -> float:
        return max(0.0, 100.0 - self.ear_bvp)

    def merge(self, payload: dict) -> None:
        for field, aliases in TELEMETRY_ALIASES.items():
            raw = next((payload[key] for key in aliases if key in payload), None)
            if raw is None:
                continue
            if field == "posture":
                if isinstance(raw, (int, float)) or str(raw).isdigit():
                    self.posture = POSTURE_CODES.get(int(raw), "SITTING")
                else:
                    value = str(raw).upper()
                    self.posture = value if value in ("LYING", "SITTING", "STANDING") else "SITTING"
            elif field == "bpm":
                self.bpm = int(float(raw))
            elif field in ("systolic", "diastolic", "battery_ear", "battery_wrist", "battery_sleeve"):
                setattr(self, field, int(float(raw)))
            else:
                setattr(self, field, float(raw))


class EventStore:
    """A local SQLite store. It retains 200 telemetry events."""
    def __init__(self) -> None:
        self.connection = sqlite3.connect(DB_PATH)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS telemetry_events (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, posture TEXT NOT NULL,
            bvp_drop REAL NOT NULL, bpm INTEGER NOT NULL, systolic INTEGER NOT NULL,
            diastolic INTEGER NOT NULL, severity TEXT NOT NULL, note TEXT NOT NULL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL)""")
        self.connection.commit()

    def add_event(self, t: Telemetry, severity: str, note: str) -> None:
        self.connection.execute(
            "INSERT INTO telemetry_events(created_at, posture, bvp_drop, bpm, systolic, diastolic, severity, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), t.posture, t.drop_percent, t.bpm, t.systolic, t.diastolic, severity, note),
        )
        self.connection.execute("DELETE FROM telemetry_events WHERE id NOT IN (SELECT id FROM telemetry_events ORDER BY id DESC LIMIT 200)")
        self.connection.commit()

    def latest_events(self, limit: int = 100) -> list[tuple]:
        return self.connection.execute(
            "SELECT created_at, posture, bvp_drop, bpm, systolic, diastolic, severity, note FROM telemetry_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def latest_intervention(self) -> tuple | None:
        return self.connection.execute(
            "SELECT created_at, posture, bvp_drop, bpm, systolic, diastolic, severity, note FROM telemetry_events WHERE severity IN ('HAZARD', 'SOS', 'TEST') ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def add_chat(self, sender: str, text: str) -> None:
        self.connection.execute("INSERT INTO chat_messages(created_at, sender, text) VALUES (?, ?, ?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sender, text))
        self.connection.commit()

    def chat_history(self) -> list[tuple]:
        return self.connection.execute("SELECT sender, text FROM chat_messages ORDER BY id DESC LIMIT 60").fetchall()[::-1]

    def close(self) -> None:
        self.connection.close()


def decode_text_payload(text: str) -> dict | None:
    text = text.strip().lstrip("\x00")
    if not text:
        return None
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) >= 6:
        try:
            return {
                "ear_bvp": float(parts[0]),
                "bpm": int(float(parts[1])),
                "systolic": int(float(parts[2])),
                "diastolic": int(float(parts[3])),
                "accel_g": float(parts[4]),
                "posture": parts[5],
            }
        except ValueError:
            return None
    return None


def decode_ble_payload(data: bytearray) -> dict | None:
    raw = bytes(data)
    if len(raw) >= 13 and raw[0] == 1:
        try:
            _ver, bvp10, bpm, systolic, diastolic, accel100, posture, batt_ear, batt_wrist, batt_sleeve = struct.unpack_from("<BHHBBHBBBB", raw)
            return {
                "ear_bvp": bvp10 / 10.0,
                "bpm": bpm,
                "systolic": systolic,
                "diastolic": diastolic,
                "accel_g": accel100 / 100.0,
                "posture": POSTURE_CODES.get(posture, "SITTING"),
                "battery_ear": batt_ear,
                "battery_wrist": batt_wrist,
                "battery_sleeve": batt_sleeve,
            }
        except struct.error:
            pass
    try:
        return decode_text_payload(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return None


class JsonAssembler:
    """Reassembles JSON objects split across BLE packets or serial chunks."""

    def __init__(self) -> None:
        self.buffer = ""

    def feed(self, chunk: str) -> list[dict]:
        self.buffer += chunk.replace("\r", "")
        found: list[dict] = []
        while True:
            start = self.buffer.find("{")
            if start < 0:
                line, _, rest = self.buffer.partition("\n")
                if not _:
                    if len(self.buffer) > 500:
                        self.buffer = ""
                    break
                payload = decode_text_payload(line)
                if payload:
                    found.append(payload)
                self.buffer = rest
                continue
            depth = 0
            end = None
            for index in range(start, len(self.buffer)):
                if self.buffer[index] == "{":
                    depth += 1
                elif self.buffer[index] == "}":
                    depth -= 1
                    if depth == 0:
                        end = index
                        break
            if end is None:
                self.buffer = self.buffer[start:]
                break
            payload = decode_text_payload(self.buffer[start : end + 1])
            if payload:
                found.append(payload)
            self.buffer = self.buffer[end + 1 :]
        return found


class BleController:
    """Connects to the Orthostat wearable, streams telemetry, and sends haptic commands."""

    def __init__(self, messages: queue.Queue):
        self.messages = messages
        self.stop = threading.Event()
        self.commands: queue.Queue[bytes] = queue.Queue()
        self.service_uuid = os.getenv("BRAINFLOW_SERVICE_UUID", DEFAULT_SERVICE_UUID)
        self.characteristic_uuid = os.getenv("BRAINFLOW_CHARACTERISTIC_UUID", DEFAULT_TELEMETRY_UUID)
        self.command_uuid = os.getenv("BRAINFLOW_COMMAND_UUID", DEFAULT_COMMAND_UUID)
        self.device_name_filter = os.getenv("BRAINFLOW_DEVICE_NAME", "Orthostat").lower()
        self.connected = False
        self.assembler = JsonAssembler()

    def scan(self) -> None:
        if BleakScanner is None:
            self.messages.put(("ble_error", "Install optional package 'bleak' to scan for Bluetooth Low Energy devices."))
            return
        def work() -> None:
            try:
                devices = asyncio.run(BleakScanner.discover(timeout=8.0))
                named = [(d.name or "Unnamed BLE device", d.address) for d in devices]
                def is_esp32(item: tuple[str, str]) -> bool:
                    name = item[0].lower()
                    return self.device_name_filter in name or any(hint in name for hint in ESP32_NAME_HINTS)
                preferred = [item for item in named if is_esp32(item)]
                others = [item for item in named if item not in preferred]
                self.messages.put(("ble_results", preferred + others))
            except Exception as exc:
                self.messages.put(("ble_error", f"BLE scan failed: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def send_haptic(self, preset: str) -> None:
        self.commands.put(bytes([HAPTIC_CODES.get(preset, 2)]))

    def cancel_haptic(self) -> None:
        self.commands.put(bytes([0]))

    def connect(self, address: str) -> None:
        if BleakClient is None:
            self.messages.put(("ble_error", "Install optional package 'bleak' before connecting a wearable."))
            return
        self.stop.set()
        self.stop = threading.Event()
        stop = self.stop
        while not self.commands.empty():
            self.commands.get_nowait()
        def work() -> None:
            async def session() -> None:
                retries = 0
                while not stop.is_set() and retries < 6:
                    try:
                        async with BleakClient(address, timeout=20.0) as client:
                            retries = 0
                            notify_uuid, write_uuid = self._resolve_characteristics(client)
                            if not notify_uuid:
                                self.messages.put(("ble_error", "Connected, but no telemetry notify characteristic was found. Flash firmware/orthostat_esp32.ino or firmware/orthostat_esp32_nus.ino."))
                                return
                            try:
                                await client._backend_start_notify  # type: ignore
                            except Exception:
                                pass
                            try:
                                await client._client  # type: ignore
                            except Exception:
                                pass
                            try:
                                await asyncio.wait_for(client._backend.exchange_mtu(185), timeout=2)  # type: ignore
                            except Exception:
                                pass
                            self.assembler = JsonAssembler()
                            await client.start_notify(notify_uuid, self._notification)
                            self.connected = True
                            self.messages.put(("ble_connected", f"{address} · notify {notify_uuid}"))
                            while client.is_connected and not stop.is_set():
                                try:
                                    payload = self.commands.get_nowait()
                                except queue.Empty:
                                    payload = None
                                if payload is not None and write_uuid:
                                    await client.write_gatt_char(write_uuid, payload, response=False)
                                await asyncio.sleep(0.15)
                    except Exception as exc:
                        self.connected = False
                        retries += 1
                        self.messages.put(("ble_error", f"BLE connection issue ({retries}/6): {exc}"))
                        await asyncio.sleep(2)
                    else:
                        self.connected = False
                        if stop.is_set():
                            break
                        retries += 1
                        await asyncio.sleep(2)
                self.connected = False
                self.messages.put(("ble_disconnected", address))
            asyncio.run(session())
        threading.Thread(target=work, daemon=True).start()

    def _resolve_characteristics(self, client) -> tuple[str | None, str | None]:
        notify_uuid, write_uuid = None, None
        wanted_notify = self.characteristic_uuid.lower()
        wanted_write = self.command_uuid.lower()
        fallback_notify, fallback_write = None, None
        for service in client.services:
            service_id = str(service.uuid).lower()
            for char in service.characteristics:
                char_id = str(char.uuid).lower()
                props = {p.lower() for p in char.properties}
                if char_id == wanted_notify and ("notify" in props or "indicate" in props):
                    notify_uuid = char.uuid
                if char_id == wanted_write and ("write" in props or "write-without-response" in props):
                    write_uuid = char.uuid
                if service_id in STANDARD_BLE_SERVICES:
                    continue
                if fallback_notify is None and ("notify" in props or "indicate" in props):
                    fallback_notify = char.uuid
                if fallback_write is None and ("write" in props or "write-without-response" in props):
                    fallback_write = char.uuid
        return notify_uuid or fallback_notify, write_uuid or fallback_write

    def _notification(self, _sender: object, data: bytearray) -> None:
        payload = decode_ble_payload(data)
        if payload is None:
            self.messages.put(("ble_error", "Ignoring unrecognised BLE payload. Firmware must send Orthostat binary v1 or UTF-8 JSON."))
            return
        self.messages.put(("ble_telemetry", payload))


class Coach:
    """A safety-bounded chat coach with a free, local Ollama backend."""

    @staticmethod
    def system_prompt(t: Telemetry, event: tuple | None) -> str:
        return (
            "You are BrainFlow Guard, a warm informational health-coaching companion. "
            "Have a natural, concise conversation and use the supplied live telemetry when relevant. "
            "Do not diagnose, prescribe, minimize urgent symptoms, or claim to contact emergency services. "
            "For severe or worsening symptoms (for example fainting, chest pain, trouble breathing, or stroke-like symptoms), "
            "tell the user to seek urgent local medical help. Always say that you are not a medical professional when giving health information. "
            f"Current telemetry: posture={t.posture}; Ear PPG drop={t.drop_percent:.1f}%; heart rate={t.bpm} BPM; "
            f"blood pressure={t.systolic}/{t.diastolic} mmHg. Latest safety event={event}."
        )

    @staticmethod
    def connection_status() -> tuple[str, str]:
        """Return a short UI-ready status without depending on the Ollama CLI."""
        url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
        try:
            with urllib.request.urlopen(f"{url}/api/tags", timeout=3) as response:
                models = json.loads(response.read().decode()).get("models", [])
            names = {item.get("name") for item in models if isinstance(item, dict)}
            if model in names:
                return (f"LOCAL AI READY  ·  {model}", TEAL)
            return (f"LOCAL AI ONLINE  ·  model '{model}' is not installed", AMBER)
        except Exception:
            return ("LOCAL AI OFFLINE  ·  start Ollama, then restart the app", RED)

    @staticmethod
    def local_answer(question: str, t: Telemetry, event: tuple | None) -> str:
        q = question.lower()
        if any(word in q for word in ("sleeve", "haptic", "buzz", "fire")):
            if event:
                return f"The latest recorded intervention was at {event[0]}, during {event[1]}. Ear PPG was down {event[2]:.1f}% and heart rate was {event[3]} BPM, so the monitor marked it {event[6]}."
            return "No intervention is stored yet. Haptics trigger when standing Ear PPG drop exceeds the configured threshold."
        if any(word in q for word in ("status", "health", "biometric", "reading")):
            return f"Current local reading: {t.bpm} BPM, {t.systolic}/{t.diastolic} mmHg, {t.drop_percent:.1f}% Ear PPG drop, posture {t.posture}. This is not medical advice."
        return "I can summarize locally stored telemetry and interventions. Ask about current readings, haptics, or safety events. This is informational, not medical advice."

    @staticmethod
    def ask(question: str, t: Telemetry, event: tuple | None, history: list[tuple]) -> str:
        """Prefer Ollama: it is free, local, and needs no account or API key."""
        ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        ollama_model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
        messages = [{"role": "system", "content": Coach.system_prompt(t, event)}]
        for sender, text in history[-12:]:
            messages.append({"role": "user" if sender == "You" else "assistant", "content": text})
        messages.append({"role": "user", "content": question})
        body = json.dumps({"model": ollama_model, "messages": messages, "stream": False}).encode()
        request = urllib.request.Request(f"{ollama_url}/api/chat", data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                answer = json.loads(response.read().decode()).get("message", {}).get("content", "").strip()
                if answer:
                    return answer
                raise RuntimeError("Ollama returned an empty response")
        except Exception as ollama_error:
            return Coach.ask_gemini_or_local(question, t, event, ollama_error)

    @staticmethod
    def ask_gemini_or_local(question: str, t: Telemetry, event: tuple | None, ollama_error: Exception) -> str:
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key or api_key.startswith("YOUR_"):
            return ("The free AI coach is not running yet. Install Ollama, then run `ollama pull llama3.2:3b` in PowerShell and restart this app. "
                    f"Until then: {Coach.local_answer(question, t, event)}")
        prompt = f"{Coach.system_prompt(t, event)} User question: {question}"
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={urllib.parse.quote(api_key)}"
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            return f"The AI coach is unavailable ({exc}; local AI: {ollama_error}). {Coach.local_answer(question, t, event)}"


class RoundedButton(tk.Canvas):
    """A lightweight canvas button with consistently soft, modern corners."""

    def __init__(self, master: tk.Misc, text: str, command, *, quiet: bool = False, surface: str = CARD) -> None:
        super().__init__(master, height=42, bg=surface, highlightthickness=0, bd=0, cursor="hand2", takefocus=True)
        self.text, self.command, self.quiet, self.surface = text, command, quiet, surface
        self.normal = CARD_2 if quiet else TEXT
        self.hover = "#353535" if quiet else "#ffffff"
        self.fg = TEXT if quiet else BG
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda _e: self._draw(fill=self.hover))
        self.bind("<Leave>", lambda _e: self._draw())
        self.bind("<Button-1>", lambda _e: self.command())
        self.bind("<Return>", lambda _e: self.command())
        self.bind("<space>", lambda _e: self.command())

    def _draw(self, _event=None, fill: str | None = None) -> None:
        self.delete("all")
        width, height, radius = max(2, self.winfo_width()), max(2, self.winfo_height()), 15
        color = fill or self.normal
        self.create_arc(0, 0, radius * 2, radius * 2, start=90, extent=90, fill=color, outline=color)
        self.create_arc(width - radius * 2, 0, width, radius * 2, start=0, extent=90, fill=color, outline=color)
        self.create_arc(0, height - radius * 2, radius * 2, height, start=180, extent=90, fill=color, outline=color)
        self.create_arc(width - radius * 2, height - radius * 2, width, height, start=270, extent=90, fill=color, outline=color)
        self.create_rectangle(radius, 0, width - radius, height, fill=color, outline=color)
        self.create_rectangle(0, radius, width, height - radius, fill=color, outline=color)
        self.create_text(width / 2, height / 2, text=self.text, fill=self.fg, font=("Segoe UI", 10, "bold"))


class RoundedBadge(tk.Canvas):
    def __init__(self, master: tk.Misc, variable: tk.StringVar) -> None:
        super().__init__(master, width=150, height=34, bg=BG, highlightthickness=0, bd=0)
        self.variable, self.fill = variable, GREEN
        self.variable.trace_add("write", lambda *_args: self.draw())
        self.bind("<Configure>", lambda _e: self.draw())
        self.draw()

    def configure_badge(self, fill: str) -> None:
        self.fill = fill; self.draw()

    def draw(self) -> None:
        self.delete("all"); width, height, radius = max(2, self.winfo_width()), max(2, self.winfo_height()), 17
        for x, y, start in ((0, 0, 90), (width - radius * 2, 0, 0), (0, height - radius * 2, 180), (width - radius * 2, height - radius * 2, 270)):
            self.create_arc(x, y, x + radius * 2, y + radius * 2, start=start, extent=90, fill=self.fill, outline=self.fill)
        self.create_rectangle(radius, 0, width - radius, height, fill=self.fill, outline=self.fill)
        self.create_rectangle(0, radius, width, height - radius, fill=self.fill, outline=self.fill)
        self.create_text(width / 2, height / 2, text=self.variable.get(), fill=BG, font=("Segoe UI", 9, "bold"))


class BrainFlowApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        load_dotenv()
        self.title("BrainFlow Guard | Desktop Monitor")
        self.geometry("1220x790")
        self.minsize(980, 680)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.store, self.messages = EventStore(), queue.Queue()
        self.ble, self.telemetry = BleController(self.messages), Telemetry()
        self.user_email, self.user_role = "", "Patient"
        self.posture = tk.StringVar(value="SITTING")
        self.bvp_threshold, self.g_threshold = tk.DoubleVar(value=15), tk.DoubleVar(value=1.8)
        self.haptic_preset, self.ble_status = tk.StringVar(value="Medium"), tk.StringVar(value="DEMO MODE · LOCAL STREAM")
        self.safety_state = tk.StringVar(value="MONITORING")
        self.hazard_seconds: int | None = None
        self.override_seconds, self.escalated, self.tick = 0, False, 0
        self.history, self.last_hazard_log, self.active_screen = [], 0.0, "Dashboard"
        self.live_ble_telemetry = False
        self._style(); self.show_login(); self.after(150, self.poll_messages)

    def _style(self) -> None:
        style = ttk.Style(self); style.theme_use("clam")
        style.configure("TFrame", background=BG); style.configure("Card.TFrame", background=CARD, relief="flat")
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10)); style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 25)); style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("Metric.TLabel", background=CARD, foreground=TEAL, font=("Segoe UI", 24, "bold")); style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground=CARD_2, foreground=TEXT, insertcolor=TEXT, borderwidth=0, padding=10)
        style.configure("TCombobox", fieldbackground=CARD_2, background=CARD_2, foreground=TEXT, padding=8, borderwidth=0)
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=32, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=CARD_2, foreground=TEXT, relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#343434")], foreground=[("selected", TEXT)])

    def clear(self) -> None:
        for child in self.winfo_children(): child.destroy()

    def show_login(self) -> None:
        self.clear(); holder = ttk.Frame(self, padding=30); holder.place(relx=.5, rely=.5, anchor="center")
        ttk.Label(holder, text="BrainFlow Guard", style="Title.TLabel").pack(); ttk.Label(holder, text="LOCAL ORTHOSTATIC SAFETY MONITOR", style="Sub.TLabel").pack(pady=(2, 24))
        card = ttk.Frame(holder, style="Card.TFrame", padding=28); card.pack(fill="x")
        ttk.Label(card, text="Connect to core hub", style="Card.TLabel", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(card, text="Local demonstration access — no account is sent to a server.", style="Muted.TLabel").pack(anchor="w", pady=(3, 18))
        self.email_input, self.password_input, self.role_input = ttk.Entry(card, width=42), ttk.Entry(card, width=42, show="•"), tk.StringVar(value="Patient")
        for label, input_widget in (("Email", self.email_input), ("Access code", self.password_input)):
            ttk.Label(card, text=label, style="Card.TLabel").pack(anchor="w", pady=(10, 4)); input_widget.pack(fill="x")
        ttk.Label(card, text="Role", style="Card.TLabel").pack(anchor="w", pady=(10, 4)); ttk.Combobox(card, textvariable=self.role_input, state="readonly", values=("Patient", "Caregiver")).pack(fill="x")
        RoundedButton(card, "CONNECT TO CORE HUB", self.authenticate).pack(fill="x", pady=(22, 0)); RoundedButton(card, "DEMO QUICK ACCESS", lambda: self.authenticate(True), quiet=True).pack(fill="x", pady=(8, 0))

    def authenticate(self, demo: bool = False) -> None:
        email, password = self.email_input.get().strip(), self.password_input.get()
        if demo: email, password = "demo.patient@brainflow.local", "demo-access"
        if "@" not in email or len(password) < 8:
            messagebox.showerror("Connection", "Enter an email address and an access code of at least 8 characters."); return
        self.user_email, self.user_role = email, self.role_input.get(); self.show_app(); self.after(250, self.update_stream)

    def show_app(self) -> None:
        self.clear(); header = ttk.Frame(self, padding=(24, 18, 24, 10)); header.pack(fill="x")
        ttk.Label(header, text="BrainFlow Guard", style="Title.TLabel").pack(side="left"); ttk.Label(header, text=f"{self.user_role.upper()} · {self.user_email}", style="Sub.TLabel").pack(side="left", padx=15, pady=10)
        self.badge = RoundedBadge(header, self.safety_state); self.badge.pack(side="right")
        main = ttk.Frame(self, padding=(24, 4, 24, 24)); main.pack(fill="both", expand=True); nav = ttk.Frame(main, style="Card.TFrame", padding=12); nav.pack(side="left", fill="y", padx=(0, 16))
        for page in ("Dashboard", "Live telemetry", "Safety controls", "AI coach", "Devices & system"):
            RoundedButton(nav, page, lambda p=page: self.navigate(p), quiet=True).pack(fill="x", pady=4)
        ttk.Label(nav, text="", style="Card.TLabel").pack(expand=True); RoundedButton(nav, "Sign out", self.show_login, quiet=True).pack(fill="x")
        self.content = ttk.Frame(main); self.content.pack(side="left", fill="both", expand=True); self.navigate("Dashboard")

    def navigate(self, screen: str) -> None:
        self.active_screen = screen
        for child in self.content.winfo_children(): child.destroy()
        {"Dashboard": self.dashboard, "Live telemetry": self.live_telemetry, "Safety controls": self.safety_controls, "AI coach": self.ai_coach, "Devices & system": self.devices}[screen]()

    def page_title(self, title: str, subtitle: str) -> None:
        ttk.Label(self.content, text=title, style="Title.TLabel").pack(anchor="w"); ttk.Label(self.content, text=subtitle, style="Sub.TLabel").pack(anchor="w", pady=(1, 15))

    def dashboard(self) -> None:
        self.page_title("Command center", "Live local telemetry and safety-state overview"); row = ttk.Frame(self.content); row.pack(fill="x"); self.metrics = {}
        for key, label in (("drop", "EAR PPG DROP"), ("bpm", "HEART RATE"), ("bp", "BLOOD PRESSURE"), ("posture", "POSTURE")):
            card = ttk.Frame(row, style="Card.TFrame", padding=15); card.pack(side="left", fill="x", expand=True, padx=(0, 8)); ttk.Label(card, text=label, style="Muted.TLabel").pack(anchor="w")
            var = self.metrics[key] = tk.StringVar(value="—"); ttk.Label(card, textvariable=var, style="Metric.TLabel").pack(anchor="w", pady=(7, 0))
        graph = ttk.Frame(self.content, style="Card.TFrame", padding=16); graph.pack(fill="both", expand=True, pady=(14, 0)); ttk.Label(graph, text="Live cranial perfusion", style="Card.TLabel", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(graph, text="Ear PPG amplitude — light trace; dashed line indicates the alert threshold", style="Muted.TLabel").pack(anchor="w"); self.graph = tk.Canvas(graph, bg=CARD, highlightthickness=0, height=240); self.graph.pack(fill="both", expand=True, pady=(8, 0))
        self.safety_card = tk.Label(self.content, bg=CARD, fg=MUTED, anchor="w", justify="left", font=("Segoe UI", 10), padx=16, pady=13); self.safety_card.pack(fill="x", pady=(14, 0)); self.refresh_dashboard()

    def live_telemetry(self) -> None:
        self.page_title("Live telemetry", "Latest 200 locally retained telemetry and intervention records"); box = ttk.Frame(self.content, style="Card.TFrame", padding=12); box.pack(fill="both", expand=True)
        columns = ("time", "posture", "drop", "bpm", "bp", "severity", "note"); self.event_table = ttk.Treeview(box, columns=columns, show="headings", height=19)
        for column, title, width in zip(columns, ("Time", "Posture", "PPG drop", "BPM", "BP", "State", "Note"), (145, 90, 85, 65, 90, 100, 390)):
            self.event_table.heading(column, text=title); self.event_table.column(column, width=width, stretch=column == "note")
        self.event_table.pack(fill="both", expand=True); self.refresh_event_table()

    def safety_controls(self) -> None:
        self.page_title("Safety controls", "Tune local safety rules and acknowledge emergency workflows"); card = ttk.Frame(self.content, style="Card.TFrame", padding=20); card.pack(fill="x")
        ttk.Label(card, text="Ear PPG drop alert threshold", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w"); ttk.Scale(card, from_=5, to=40, variable=self.bvp_threshold, orient="horizontal").pack(fill="x", pady=(10, 0)); self.threshold_text = ttk.Label(card, style="Card.TLabel"); self.threshold_text.pack(anchor="w")
        ttk.Label(card, text="Posture / test scenario", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(22, 5)); ttk.Combobox(card, textvariable=self.posture, values=("LYING", "SITTING", "STANDING"), state="readonly").pack(fill="x")
        ttk.Label(card, text="Posture acceleration threshold", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(20, 5)); ttk.Scale(card, from_=1.0, to=3.5, variable=self.g_threshold, orient="horizontal").pack(fill="x")
        ttk.Label(card, text="Haptic intervention strength", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(20, 5)); ttk.Combobox(card, textvariable=self.haptic_preset, values=("Low", "Medium", "High"), state="readonly").pack(fill="x")
        RoundedButton(card, "Test haptic intervention", self.haptic_test).pack(fill="x", pady=(20, 0)); RoundedButton(card, "Cancel emergency / return to monitoring", self.reset_emergency, quiet=True).pack(fill="x", pady=(8, 0))

    def ai_coach(self) -> None:
        self.page_title("AI coaching companion", "Free private AI chat via Ollama; it includes your live telemetry in each reply"); card = ttk.Frame(self.content, style="Card.TFrame", padding=16); card.pack(fill="both", expand=True)
        coach_status, coach_color = Coach.connection_status()
        tk.Label(card, text=coach_status, bg=CARD, fg=coach_color, font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, 10))
        self.chat = tk.Text(card, bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat", wrap="word", font=("Segoe UI", 10), state="disabled"); self.chat.pack(fill="both", expand=True)
        history = self.store.chat_history()
        for sender, text in history: self.write_chat(sender, text)
        if not history: self.write_chat("Coach", "Hello. I can chat about your readings, haptics, and safety events. I use Ollama locally when it is installed; I am not a medical professional.")
        bottom = ttk.Frame(card, style="Card.TFrame"); bottom.pack(fill="x", pady=(12, 0)); self.chat_input = ttk.Entry(bottom); self.chat_input.pack(side="left", fill="x", expand=True, padx=(0, 8)); self.chat_input.bind("<Return>", lambda _e: self.send_chat()); RoundedButton(bottom, "Send", self.send_chat).pack(side="right")

    def devices(self) -> None:
        self.page_title("Devices & system", "Bluetooth transport, power estimates, and local configuration"); card = ttk.Frame(self.content, style="Card.TFrame", padding=20); card.pack(fill="x")
        tk.Label(card, textvariable=self.ble_status, bg=CARD, fg=TEAL, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x"); RoundedButton(card, "Scan for BLE devices", self.scan_ble).pack(fill="x", pady=(16, 8))
        self.device_picker = ttk.Combobox(card, state="readonly", values=("No device selected",)); self.device_picker.current(0); self.device_picker.pack(fill="x"); RoundedButton(card, "Connect selected device", self.connect_ble, quiet=True).pack(fill="x", pady=(8, 0))
        ttk.Label(card, text="Flash firmware/orthostat_esp32.ino so the wearable advertises as Orthostat. The app then streams live telemetry and writes haptic commands over BLE. Bluetooth must run on this computer, not in the cloud.", style="Muted.TLabel", wraplength=650).pack(anchor="w", pady=(18, 0))
        batt = ttk.Frame(self.content, style="Card.TFrame", padding=20); batt.pack(fill="x", pady=(14, 0)); ttk.Label(batt, text="Estimated device battery", style="Card.TLabel", font=("Segoe UI", 12, "bold")).pack(anchor="w"); self.battery = ttk.Label(batt, style="Card.TLabel"); self.battery.pack(anchor="w", pady=(10, 0)); self.refresh_devices()

    def update_stream(self) -> None:
        if not hasattr(self, "content") or not self.content.winfo_exists(): return
        self.tick += 1; posture, phase = self.posture.get(), self.tick * .18
        if self.live_ble_telemetry:
            t = self.telemetry
        elif posture == "STANDING": t = Telemetry(12 + math.sin(phase) * 2, 110 + int(math.sin(phase) * 6), 85, 55, 2.4, posture)
        elif posture == "LYING": t = Telemetry(98 + math.sin(phase) * 1.5, 64 + int(math.sin(phase) * 2), 115, 75, .95, posture)
        else: t = Telemetry(96 + math.sin(phase) * 3, 70 + int(math.sin(phase) * 4), 120, 80, 1.0, posture)
        if not self.live_ble_telemetry:
            t.battery_ear, t.battery_wrist, t.battery_sleeve = max(10, 94 - self.tick // 120), max(10, 98 - self.tick // 120), max(10, 89 - self.tick // 120)
        self.telemetry = t; self.history = (self.history + [t.ear_bvp])[-120:]; self.run_safety_logic(); self.refresh_active_page(); self.after(250, self.update_stream)

    def run_safety_logic(self) -> None:
        t = self.telemetry; hazardous = t.posture == "STANDING" and t.drop_percent > self.bvp_threshold.get()
        if hazardous and self.hazard_seconds is None and self.override_seconds == 0 and not self.escalated:
            self.hazard_seconds = 30
            self.safety_state.set("HAZARD DETECTED")
            self.ble.send_haptic(self.haptic_preset.get())
            self.log_event("HAZARD", f"{self.haptic_preset.get()} haptic intervention activated")
        if not hazardous and self.hazard_seconds is not None: self.reset_emergency(False)
        if self.tick % 4 != 0: return
        if self.hazard_seconds is not None:
            self.hazard_seconds -= 1
            if self.hazard_seconds <= 0:
                self.hazard_seconds, self.override_seconds = None, 15
                self.safety_state.set("EMERGENCY PENDING")
        elif self.override_seconds > 0:
            self.override_seconds -= 1
            if self.override_seconds <= 0:
                self.escalated = True
                self.safety_state.set("SOS REVIEW REQUIRED")
                self.log_event("SOS", "Escalation reached; the app does not call or text emergency services automatically")

    def safety_message(self) -> tuple[str, str, str]:
        if self.hazard_seconds is not None: return (f"HAZARD DETECTED · {self.haptic_preset.get()} haptics active · SOS review in {self.hazard_seconds}s", CARD_2, TEXT)
        if self.override_seconds: return (f"EMERGENCY PENDING · cancel escalation within {self.override_seconds}s", "#3a3a3a", TEXT)
        if self.escalated: return ("SOS REVIEW REQUIRED · Contact local emergency services manually.", "#4a4a4a", TEXT)
        return ("Monitoring normally. The app logs events locally and does not contact emergency services automatically.", CARD, MUTED)

    def refresh_active_page(self) -> None:
        if self.active_screen == "Dashboard" and hasattr(self, "metrics"): self.refresh_dashboard()
        elif self.active_screen == "Live telemetry" and hasattr(self, "event_table"): self.refresh_event_table()
        elif self.active_screen == "Safety controls" and hasattr(self, "threshold_text"): self.threshold_text.configure(text=f"Trigger at {self.bvp_threshold.get():.0f}% Ear PPG drop · {self.g_threshold.get():.1f}G posture threshold")
        elif self.active_screen == "Devices & system" and hasattr(self, "battery"): self.refresh_devices()

    def refresh_dashboard(self) -> None:
        t = self.telemetry
        for key, value in {"drop": f"{t.drop_percent:.1f}%", "bpm": f"{t.bpm} BPM", "bp": f"{t.systolic} / {t.diastolic}", "posture": t.posture}.items(): self.metrics[key].set(value)
        message, bg, fg = self.safety_message(); self.safety_card.configure(text=message, bg=bg, fg=fg); self.badge.configure_badge(GREEN if self.safety_state.get() == "MONITORING" else (RED if "EMERGENCY" in self.safety_state.get() or "SOS" in self.safety_state.get() else AMBER)); self.draw_graph()

    def draw_graph(self) -> None:
        canvas = self.graph; canvas.delete("all"); width, height = max(420, canvas.winfo_width()), max(190, canvas.winfo_height()); canvas.create_line(0, height - (self.bvp_threshold.get() / 45 * height), width, height - (self.bvp_threshold.get() / 45 * height), fill=AMBER, dash=(5, 4))
        if len(self.history) < 2: return
        points = []
        for index, value in enumerate(self.history): points += [index / (len(self.history) - 1) * width, height - value / 105 * (height - 16) - 8]
        canvas.create_line(points, fill=TEAL, width=2, smooth=True)

    def refresh_event_table(self) -> None:
        for item in self.event_table.get_children(): self.event_table.delete(item)
        for created, posture, drop, bpm, sys, dia, severity, note in self.store.latest_events(): self.event_table.insert("", "end", values=(created, posture, f"{drop:.1f}%", bpm, f"{sys}/{dia}", severity, note))

    def refresh_devices(self) -> None:
        t = self.telemetry; self.battery.configure(text=f"Ear PPG sensor  {t.battery_ear}%    •    Wrist hub  {t.battery_wrist}%    •    Compression sleeves  {t.battery_sleeve}%")

    def log_event(self, severity: str, note: str) -> None:
        now = time.monotonic()
        if severity == "HAZARD" and now - self.last_hazard_log < 20: return
        self.last_hazard_log = now; self.store.add_event(self.telemetry, severity, note)

    def haptic_test(self) -> None:
        self.ble.send_haptic(self.haptic_preset.get()); self.log_event("TEST", f"Manual {self.haptic_preset.get()} haptic intervention test")
        if self.ble.connected:
            messagebox.showinfo("Haptic test", f"{self.haptic_preset.get()} haptic command was sent to the connected wearable.")
        else:
            messagebox.showinfo("Haptic test", "Haptic test was logged locally. Connect the Orthostat wearable to fire the motors.")

    def reset_emergency(self, log: bool = True) -> None:
        if log: self.log_event("INFO", "Safety override acknowledged")
        self.ble.cancel_haptic()
        self.hazard_seconds, self.override_seconds, self.escalated = None, 0, False; self.safety_state.set("MONITORING")

    def scan_ble(self) -> None:
        self.ble_status.set("SCANNING FOR BLE DEVICES…"); self.ble.scan()

    def connect_ble(self) -> None:
        choice = self.device_picker.get()
        if choice in ("", "No device selected", "No BLE devices found"):
            messagebox.showinfo("Bluetooth", "Scan first, then select a BLE wearable."); return
        _name, address = choice.rsplit(" · ", 1); self.ble_status.set(f"CONNECTING · {address}"); self.ble.connect(address)

    def send_chat(self) -> None:
        question = self.chat_input.get().strip()
        if not question: return
        self.chat_input.delete(0, "end"); self.write_chat("You", question); history = self.store.chat_history(); self.store.add_chat("You", question); event, telemetry = self.store.latest_intervention(), self.telemetry
        threading.Thread(target=lambda: self.messages.put(("coach_response", Coach.ask(question, telemetry, event, history))), daemon=True).start()

    def write_chat(self, sender: str, text: str) -> None:
        if not hasattr(self, "chat"): return
        self.chat.configure(state="normal"); self.chat.insert("end", f"{sender}: ", ("sender",)); self.chat.insert("end", f"{text}\n\n"); self.chat.tag_configure("sender", foreground=TEAL, font=("Segoe UI", 10, "bold")); self.chat.configure(state="disabled"); self.chat.see("end")

    def poll_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "ble_results":
                    choices = [f"{name} · {address}" for name, address in payload] or ["No BLE devices found"]
                    if hasattr(self, "device_picker"): self.device_picker.configure(values=choices); self.device_picker.current(0)
                    self.ble_status.set(f"SCAN COMPLETE · {len(payload)} DEVICE(S) FOUND")
                elif kind == "ble_connected": self.ble_status.set(f"CONNECTED · {payload}")
                elif kind == "ble_disconnected": self.live_ble_telemetry = False; self.ble_status.set("DISCONNECTED · DEMO MODE ACTIVE")
                elif kind == "ble_telemetry":
                    self.telemetry.merge(payload)
                    self.posture.set(self.telemetry.posture)
                    self.live_ble_telemetry = True
                elif kind == "ble_error": self.ble_status.set("BLE UNAVAILABLE · DEMO MODE ACTIVE"); self.log_event("INFO", str(payload))
                elif kind == "coach_response": self.write_chat("Coach", str(payload)); self.store.add_chat("Coach", str(payload))
        except queue.Empty: pass
        if self.winfo_exists(): self.after(150, self.poll_messages)

    def close(self) -> None:
        self.ble.stop.set(); self.store.close(); self.destroy()


if __name__ == "__main__":
    BrainFlowApp().mainloop()
