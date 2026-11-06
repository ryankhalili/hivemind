#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "Install Python 3 from python.org, then open HiveMind again." buttons {"OK"}'
  exit 1
fi
if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
  osascript -e 'display dialog "This launcher needs Python 3 with Tkinter. The python.org macOS installer includes it." buttons {"OK"}'
  exit 1
fi
exec python3 app.py
