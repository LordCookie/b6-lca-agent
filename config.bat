@echo off
REM ###########################################################################
REM  B6-Bilanzierung (DIN EN 15978) - Konfigurations-Skript fuer Windows
REM
REM  Traegt den OpenRouter-API-Key SICHER in die lokale .env ein.
REM  - Eingabe wird NICHT angezeigt (PowerShell Read-Host -AsSecureString).
REM  - Schluessel wird NIE ausgegeben, NIE geloggt, NIE in eine
REM    Batch-Variable kopiert. Update der .env-Datei geschieht im
REM    selben PowerShell-Prozess.
REM
REM  Aufruf:   config.bat   (Doppelklick oder in der Eingabeaufforderung)
REM  Danach:   start.bat
REM ###########################################################################
setlocal
cd /d "%~dp0"

echo ==============================================================
echo  B6-Bilanzierung - Konfiguration
echo ==============================================================

REM --- 1) .env vorhanden? Sonst aus Vorlage erstellen --------------------
if not exist ".env" (
  if not exist ".env.example" (
    echo [FEHLER] .env.example fehlt. Repository unvollstaendig?
    goto :fail
  )
  copy /y ".env.example" ".env" >nul
  echo ==^> .env aus .env.example erstellt.
) else (
  echo ==^> .env existiert - bestehender Schluessel wird ueberschrieben.
)

echo.
echo OpenRouter-API-Key eingeben ^(die Eingabe wird NICHT angezeigt^).
echo   Holen unter: https://openrouter.ai/settings/keys
echo   Format:      sk-or-v1-...
echo.

REM --- 2) Key abfragen + .env aktualisieren -- alles in PowerShell, --------
REM     so verlaesst der Klartext NIE den PS-Prozess.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $sec = Read-Host -Prompt 'OPENROUTER_API_KEY' -AsSecureString; $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec); try { $key = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }; if ([string]::IsNullOrWhiteSpace($key)) { Write-Host '[FEHLER] Leerer Schluessel - abgebrochen.' -ForegroundColor Red; exit 1 }; if (-not $key.StartsWith('sk-or-')) { Write-Host '[!] Schluessel beginnt nicht mit sk-or-. Sicher, dass es ein OpenRouter-Key ist?' -ForegroundColor Yellow; $ans = Read-Host 'Trotzdem speichern? [j/N]'; if ($ans -notmatch '^[jJyY]') { Write-Host 'Abgebrochen.' -ForegroundColor Red; exit 1 } }; $envFile = Join-Path $PWD '.env'; $lines = @(Get-Content -LiteralPath $envFile); $found = $false; $newLines = foreach ($l in $lines) { if ($l -match '^OPENROUTER_API_KEY=') { $found = $true; 'OPENROUTER_API_KEY=' + $key } else { $l } }; if (-not $found) { $newLines = @($newLines) + ('OPENROUTER_API_KEY=' + $key) }; $enc = New-Object System.Text.UTF8Encoding $false; [System.IO.File]::WriteAllLines($envFile, [string[]]$newLines, $enc); Remove-Variable key; Write-Host ''; Write-Host '==> OPENROUTER_API_KEY in .env gesetzt.' -ForegroundColor Green"

if errorlevel 1 goto :fail

echo.
echo Weitere Einstellungen ^(REFERENCE_YEAR, WEB_SEARCH_*, VEGETATION_*, ...^)
echo kannst du direkt in der Datei .env editieren - alle haben sinnvolle Defaults.
echo.
echo Naechster Schritt:
echo   start.bat      ^(Tool bauen und starten^)
echo.
endlocal
pause
exit /b 0

:fail
echo.
echo Konfiguration abgebrochen.
endlocal
pause
exit /b 1
