[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonCommand = Get-Command py -ErrorAction SilentlyContinue
if ($null -eq $PythonCommand) { $PythonCommand = Get-Command python -ErrorAction SilentlyContinue }
if ($null -eq $PythonCommand) {
    throw 'Python 3.10 or later was not found. Install it from https://www.python.org/downloads/ and enable Add Python to PATH.'
}

$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host 'Creating local Python environment…'
    if ($PythonCommand.Name -eq 'py.exe') { & py -3 -m venv (Join-Path $ProjectRoot '.venv') }
    else { & python -m venv (Join-Path $ProjectRoot '.venv') }
}

Write-Host 'Installing or checking Bluetooth support…'
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot 'requirements.txt')
Write-Host 'Starting BrainFlow Guard…'
& $VenvPython (Join-Path $ProjectRoot 'brainflow_guard.py')
