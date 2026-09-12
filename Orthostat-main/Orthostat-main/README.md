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

## Optional configuration

Copy `.env.example` to `.env` and fill only services and hardware you use.

- `GEMINI_API_KEY` enables online coaching; without it, the coach gives local telemetry-aware answers.
- `BRAINFLOW_SERVICE_UUID` and `BRAINFLOW_CHARACTERISTIC_UUID` enable BLE notifications. The included decoder expects a UTF-8 JSON notification, e.g. `{"ear_bvp":96.5,"bpm":72,"posture":"SITTING"}`. Replace it once you have the wearable's documented payload format.

The app does not automatically call or text emergency services. It makes an escalation visible for the user/caregiver to handle. This is a prototype, not a medical device.
