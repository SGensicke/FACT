@echo off
echo ===============================
echo Starte Setup und Programm
echo ===============================

REM Prüfen ob Python installiert ist
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo Python wurde nicht gefunden.
    pause
    exit /b
)

REM Requirements installieren
echo Installiere benoetigte Bibliotheken...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

REM Python Programm starten (im Hintergrund)
echo Starte Python Programm...
start "" python app.py

REM Warten bis Server gestartet ist
timeout /t 5 >nul

REM Browser oeffnen
echo Oeffne Browser...
start http://127.0.0.1:5000

echo Fertig.