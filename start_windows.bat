@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install dependencies. Make sure Python 3.10+ is installed.
  pause
  exit /b 1
)
py -m streamlit run app.py
pause
