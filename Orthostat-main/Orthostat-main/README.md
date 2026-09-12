# BrainFlow Guard — Python desktop app

This is the complete runnable desktop implementation of BrainFlow Guard. It replaces the Android interface with a local Python/Tkinter application that runs from PowerShell. It includes local authentication, telemetry simulation, posture/PPG safety rules, haptic/SOS workflow, SQLite history, BLE scanning/transport, batteries, and a telemetry-aware coaching companion.

## Run it in PowerShell

```powershell
cd C:\Users\zeiry\Documents\Orthostat-main\Orthostat-main
.\run.ps1
```

`run.ps1` creates a local virtual environment, installs Bluetooth support, and starts the program. It uses `py` first, then `python`; install Python 3.10+ from python.org and tick **Add Python to PATH** if neither command is available.

You can also run it manually:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe brainflow_guard.py
```

## Free AI coaching chat (recommended)

The coach can have a real, private conversation using [Ollama](https://ollama.com/), which runs a model on your own computer and does not need an account or API key. Install Ollama for Windows, open a new PowerShell window, then run:

```powershell
ollama pull llama3.2:3b
```

Start the app with `./run.ps1` as usual. The first reply can take a little longer while the model loads. A computer with 8 GB+ RAM is recommended. The app sends its recent chat and current telemetry to Ollama at `http://127.0.0.1:11434`; it does not send them to a cloud service. If Ollama is not available, the coach clearly explains how to enable it and still provides limited local responses.

## Optional configuration

Copy `.env.example` to `.env` and fill only services and hardware you use.

- `OLLAMA_URL` and `OLLAMA_MODEL` override the local AI server/model (defaults: `http://127.0.0.1:11434` and `llama3.2:3b`).
- `GEMINI_API_KEY` enables the existing cloud fallback if you choose to configure it.
- `BRAINFLOW_SERVICE_UUID` and `BRAINFLOW_CHARACTERISTIC_UUID` enable BLE notifications. The included decoder expects a UTF-8 JSON notification, e.g. `{"ear_bvp":96.5,"bpm":72,"posture":"SITTING"}`. Replace it once you have the wearable's documented payload format.

## Connect your wearable (required for a real device)

Bluetooth cannot run in GitHub/cloud. The PC that runs this app must have Bluetooth, and the wearable must be nearby.

1. Flash `firmware/orthostat_esp32.ino` to an ESP32 (Arduino IDE, board: ESP32 Dev Module).
2. On the same Windows computer as this app, turn Bluetooth on.
3. Run `.\run.ps1`, sign in, open **Devices & system**, scan, select **Orthostat**, connect.
4. Live numbers should replace demo mode. Hold the ESP32 **BOOT** button to simulate standing + PPG drop; the app should buzz the motor on GPIO 26 and raise a hazard.
5. Replace `readSensors()` in the firmware with your ear PPG, IMU, and battery code. Keep the 13-byte packet (or send UTF-8 JSON with the same field names).

Packet (little-endian): `version=1`, `ear_bvp*10` (u16), `bpm` (u16), `systolic`, `diastolic`, `accel_g*100` (u16), `posture` 0=LYING 1=SITTING 2=STANDING, three battery percents.

Haptic write to the command characteristic: `0` off, `1` low, `2` medium, `3` high.

If your board is not ESP32 (nRF52, Arduino Nano 33 BLE, etc.), use the same name, UUIDs, and packet. The desktop app does not need a rewrite.

The app does not automatically call or text emergency services. It makes an escalation visible for the user/caregiver to handle. This is a prototype, not a medical device.
