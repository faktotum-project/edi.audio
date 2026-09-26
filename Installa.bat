@echo off
setlocal enabledelayedexpansion
title DJ Downloader — Installazione

echo.
echo  =====================================================
echo   DJ Downloader — Installazione automatica Windows
echo  =====================================================
echo.

:: ---- 1. Controlla Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo  [1/4] Python non trovato. Scarico e installo Python 3.12...
    echo.
    powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile '%TEMP%\python_installer.exe'"
    echo  Avvio installer Python. IMPORTANTE: metti la spunta su "Add python.exe to PATH"!
    start /wait "" "%TEMP%\python_installer.exe" /passive PrependPath=1
    del "%TEMP%\python_installer.exe"
    echo.
    echo  Python installato. Riavvio script per applicare PATH...
    start "" "%~f0"
    exit
) else (
    echo  [1/4] Python trovato. OK
)

:: ---- 2. Controlla / installa ffmpeg ----
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo  [2/4] ffmpeg non trovato. Installo tramite winget...
    winget install --id=Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo.
        echo  winget non disponibile. Scarico ffmpeg manualmente...
        powershell -Command "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile '%TEMP%\ffmpeg.zip'"
        powershell -Command "Expand-Archive -Path '%TEMP%\ffmpeg.zip' -DestinationPath 'C:\ffmpeg' -Force"
        del "%TEMP%\ffmpeg.zip"
        :: Trova la cartella bin dentro C:\ffmpeg (ha un nome con la versione)
        for /d %%i in ("C:\ffmpeg\ffmpeg-*") do set FFBIN=%%i\bin
        if defined FFBIN (
            powershell -Command "[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','Machine') + ';!FFBIN!', 'Machine')"
            set "PATH=%PATH%;!FFBIN!"
            echo  ffmpeg installato in !FFBIN!
        )
    )
    echo  [2/4] ffmpeg installato. OK
) else (
    echo  [2/4] ffmpeg trovato. OK
)

:: ---- 3. Crea virtualenv Python ----
if not exist ".venv" (
    echo  [3/4] Creo l'ambiente virtuale Python (.venv)...
    python -m venv .venv
) else (
    echo  [3/4] Ambiente virtuale gia' esistente. OK
)

:: ---- 4. Installa dipendenze ----
echo  [4/4] Installo dipendenze (yt-dlp, spotdl)...
.venv\Scripts\python -m pip install --upgrade pip --quiet
.venv\Scripts\pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo  ERRORE durante l'installazione delle dipendenze.
    echo  Controlla la connessione internet e riprova.
    pause
    exit /b 1
)

echo.
echo  =====================================================
echo   Installazione completata con successo!
echo  =====================================================
echo.
echo  Da ora in poi usa "Avvia DJ Downloader.bat" per aprire l'app.
echo.
echo  Avvio l'app ora...
timeout /t 2 /nobreak >nul
.venv\Scripts\python app.py
