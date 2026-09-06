@echo off
title FreeCAD Agent - Collaudo harness
setlocal
cd /d "%~dp0"

rem Which set of tests to run: smoke ^| vocab ^| pilot ^| w4 ^| pilot_w4 ^| all
set "FCA_SET=pilot"

rem Locate FreeCADCmd.exe (headless FreeCAD).
set "FCEXE=C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe"
if not exist "%FCEXE%" set "FCEXE=C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe"
if not exist "%FCEXE%" (
  echo  Could not find FreeCADCmd.exe at "C:\Program Files\FreeCAD 1.1\bin".
  echo  Edit this .bat and set FCEXE to the right path.
  pause ^& exit /b 1
)

echo.
echo  Running collaudo harness ^(set = %FCA_SET%^) ...
echo  This drives the REAL engine + Ollama + FreeCAD geometry headless.
echo  A small local model is slow: be patient, do not close this window.
echo.
"%FCEXE%" tools\collaudo_harness.py
echo.
echo  ============================================================
echo   Done. Results written to: tools\collaudo_out\results.md
echo  ============================================================
pause ^>nul
