Run this command in powershell to launch the app : cd C:\Users\zeiry\Documents\Orthostat-main\Orthostat-main
.\.venv\Scripts\python.exe brainflow_guard.py


Notes: 
- You must have python installed on your pc
- You need ollama


For Python:

Install Python using: winget install Python.Python.3.13 --silent --accept-package-agreements --accept-source-agreements


For Ollama:

Install ollama using: irm https://ollama.com/install.ps1 | iex

Then run this command in powershell: ollama pull llama3.2:3b

