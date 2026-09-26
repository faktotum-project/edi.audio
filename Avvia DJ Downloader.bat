@echo off
title DJ Downloader

:: Spostati nella cartella dove si trova questo file .bat
cd /d "%~dp0"

:: Controlla che l'installazione sia stata fatta
if not exist ".venv\Scripts\python.exe" (
    echo  Ambiente non trovato. Avvio prima l'installazione...
    call Installa.bat
    exit /b
)

:: Controlla ffmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ATTENZIONE: ffmpeg non trovato nel PATH.
    echo  Esegui "Installa.bat" per installarlo automaticamente.
    echo.
    pause
    exit /b 1
)

:: Avvia l'app
.venv\Scripts\python app.py
