@echo off
rem Windows laptop without Docker: double-click. The first run makes .venv and .env (a few minutes), then the site starts.
rem Stop: close this window. Update: new files over the old ones, keep data and .env, run again.
cd /d "%~dp0"
set "PY=python"
python --version >nul 2>&1 || set "PY=py -3"
%PY% --version || (echo Needs Python 3.12 or newer: https://www.python.org/downloads/ & pause & exit /b 1)

rem Labels carry PUBLIC_BASE_URL, so the first run writes this laptop's LAN address there, not localhost.
if not exist .env (
  copy .env.example .env >nul
  %PY% -c "import re,socket,pathlib;p=pathlib.Path('.env');t=p.read_text('utf-8');s=socket.socket(2,2);s.connect(('8.8.8.8',80));u='http://'+s.getsockname()[0]+':'+re.search(r'(?m)^PORT=(\S+)',t)[1];p.write_text(re.sub(r'(?m)^PUBLIC_BASE_URL=$','PUBLIC_BASE_URL='+u,t),'utf-8')" 2>nul
)

rem TZ in .env is the POSIX form for Docker; here the laptop's own time zone is used.
for /f "usebackq eol=# tokens=1* delims==" %%a in (".env") do if /i not "%%a"=="TZ" set "%%a=%%b"
rem cmd can't hold an empty value, and an empty SEMANTIC_MODEL means search by meaning is off
findstr /x "SEMANTIC_MODEL=" .env >nul && set "SEMANTIC_MODEL= "
if not defined PORT set "PORT=8000"

if not exist .venv\Scripts\python.exe %PY% -m venv .venv || (pause & exit /b 1)
fc /b requirements.txt .venv\req.txt >nul 2>&1 || (
  .venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
  copy /y requirements.txt .venv\req.txt >nul
)

echo.
if defined PUBLIC_BASE_URL echo Site: %PUBLIC_BASE_URL% - labels carry this address, so keep this laptop's IP fixed: docs/setup.md
if not defined PUBLIC_BASE_URL echo Site: http://THIS-LAPTOP-IP:%PORT% - open it by IP, not localhost: labels take the address from the browser
echo Windows asks whether Python may use the network: allow it on private networks, or phones won't reach the site.
echo.
.venv\Scripts\python -m uvicorn inventory.app:app --host 0.0.0.0 --port %PORT%
pause
