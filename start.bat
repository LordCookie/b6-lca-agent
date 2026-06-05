@echo off
REM ###########################################################################
REM  B6-Bilanzierung (DIN EN 15978) - Start-Skript fuer Windows
REM
REM  Baut die Docker-Container und startet das Tool. Reproduzierbar auf jedem
REM  Geraet mit Docker Desktop. Der OPENROUTER_API_KEY wird ausschliesslich aus
REM  der lokalen .env gelesen und NIEMALS ausgegeben oder ins Image gebacken.
REM
REM  Aufruf:   start.bat  (Doppelklick oder in der Eingabeaufforderung)
REM  Stoppen:  docker compose down
REM ###########################################################################
setlocal enabledelayedexpansion

REM Ins Verzeichnis dieses Skripts wechseln (egal von wo aufgerufen).
cd /d "%~dp0"

echo ==============================================================
echo  Cluster-Bilanzierung B6 - Betriebsenergie (DIN EN 15978)
echo ==============================================================

REM --- 1) Docker vorhanden + Daemon laeuft? --------------------------------
where docker >nul 2>&1
if errorlevel 1 (
  echo [FEHLER] Docker ist nicht installiert. Bitte Docker Desktop installieren:
  echo          https://docs.docker.com/get-docker/
  goto :fail
)
docker info >nul 2>&1
if errorlevel 1 (
  echo [FEHLER] Docker-Daemon laeuft nicht. Bitte Docker Desktop starten und erneut versuchen.
  goto :fail
)

REM docker compose v2 (Plugin) bevorzugt, sonst docker-compose v1.
set "COMPOSE="
docker compose version >nul 2>&1
if not errorlevel 1 (
  set "COMPOSE=docker compose"
) else (
  where docker-compose >nul 2>&1
  if not errorlevel 1 set "COMPOSE=docker-compose"
)
if not defined COMPOSE (
  echo [FEHLER] Docker Compose nicht gefunden. Bitte aktuelles Docker Desktop installieren.
  goto :fail
)
echo ==^> Docker gefunden - Compose-Befehl: !COMPOSE!

REM --- 2) .env vorhanden? Sonst aus Vorlage anlegen ------------------------
if not exist ".env" (
  if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo [!] .env wurde aus .env.example erstellt.
    echo [!] Bitte OPENROUTER_API_KEY in der Datei .env eintragen und Skript erneut starten.
    goto :fail
  ) else (
    echo [FEHLER] .env und .env.example fehlen beide. Repository unvollstaendig?
    goto :fail
  )
)

REM --- 3) API-Key gesetzt? (Wert wird NIE ausgegeben) ----------------------
REM findstr prueft nur auf mindestens ein Zeichen hinter dem '=' .
findstr /r /c:"^OPENROUTER_API_KEY=." ".env" >nul 2>&1
if errorlevel 1 (
  echo [FEHLER] OPENROUTER_API_KEY ist in .env leer.
  echo          Bitte den Schluessel eintragen: OPENROUTER_API_KEY=sk-or-...
  goto :fail
)
echo ==^> OPENROUTER_API_KEY ist gesetzt.

REM --- 4) Kritische Datensaetze vorhanden? ---------------------------------
set "MISSING=0"
call :check "data\deepness\tree_segmentation.onnx"                    "DEEPNESS-Vegetationsmodell"
call :check "data\imagery\AOI.tif"                                    "Luftbild DOP20"
call :check "data\oekobaudat\oekobaudat_2024-I_2026-05-25.csv"        "OEKOBAUDAT-Snapshot"
call :check "data\climate\DEU_NI_Oldenburg.102150_TMYx.2011-2025.epw" "Klimadaten TMYx"
if "!MISSING!"=="1" (
  echo [!] Mindestens ein Datensatz fehlt - Laeufe koennen fehlschlagen.
  set /p "ANS=    Trotzdem fortfahren? [j/N] "
  if /i not "!ANS!"=="j" (
    echo [FEHLER] Abgebrochen. Bitte fehlende Dateien bereitstellen.
    goto :fail
  )
) else (
  echo ==^> Alle kritischen Datensaetze vorhanden.
)

REM --- 5) Bauen + starten --------------------------------------------------
echo ==^> Baue Container und starte ^(beim ersten Mal mehrere Minuten^)...
!COMPOSE! up -d --build
if errorlevel 1 (
  echo [FEHLER] Build/Start fehlgeschlagen. Details siehe Ausgabe oben.
  goto :fail
)

REM --- 6) Auf Backend-Health warten ---------------------------------------
echo ==^> Warte auf das Backend ^(http://localhost:8000/api/health^)...
set "HEALTHY=0"
set /a TRIES=0
:healthloop
curl -fsS http://localhost:8000/api/health >nul 2>&1
if not errorlevel 1 (
  set "HEALTHY=1"
  goto :healthdone
)
set /a TRIES+=1
if !TRIES! GEQ 60 goto :healthdone
REM ping als robuster ~2s-Delay (timeout hakt in manchen Konsolen)
ping -n 3 127.0.0.1 >nul
goto :healthloop
:healthdone
if "!HEALTHY!"=="1" (
  echo ==^> Backend ist bereit.
) else (
  echo [!] Backend antwortet noch nicht ^(Timeout^). Logs pruefen mit:  !COMPOSE! logs -f backend
)

REM --- 7) Browser oeffnen + Hinweise --------------------------------------
echo ==============================================================
echo  Web-Oberflaeche:  http://localhost:5173
echo  API-Health:       http://localhost:8000/api/health
echo.
echo  Stoppen:        !COMPOSE! down
echo  Logs ansehen:   !COMPOSE! logs -f
echo ==============================================================
start "" "http://localhost:5173"
endlocal
exit /b 0

REM --- Hilfsroutine: Datei-Existenz pruefen -------------------------------
:check
if not exist "%~1" (
  echo [!] fehlt: %~1  ^(%~2^)
  set "MISSING=1"
)
goto :eof

:fail
echo.
echo Vorgang abgebrochen.
endlocal
pause
exit /b 1
