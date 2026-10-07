@echo off
title FreeCAD Agent - Automated tests (without FreeCAD)
cd /d "%~dp0"

rem --- Find a Python interpreter (first "py", then "python") ---
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE ( where python >nul 2>nul && set "PYEXE=python" )
if not defined PYEXE (
  echo  ERROR: Python not found. Install Python and try again.
  echo  https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  pause & exit /b 1
)

set "LOG=%~dp0tests_output.txt"
echo FreeCAD Agent - test run > "%LOG%"
echo Interpreter: %PYEXE% >> "%LOG%"
echo. >> "%LOG%"

echo  ============================================================
echo   FreeCAD Agent - test suite (does NOT use FreeCAD or Ollama)
echo   A full log is also written to: tests_output.txt
echo  ============================================================
echo.

set "FAIL=0"

call :runtest "1/27 Bridge basics"            tests\test_bridge_core.py
call :runtest "2/27 Command validation"       tests\test_validation.py
call :runtest "3/27 Structured round-trip"    tests\test_roundtrip.py
call :runtest "4/27 Planning brain"           tests\test_brain.py
call :runtest "5/27 Ollama client"            tests\test_ollama_client.py
call :runtest "6/27 Expanded vocabulary"      tests\test_vocabulary_exec.py
call :runtest "7/27 Document perception"      tests\test_perception.py
call :runtest "8/27 Full natural-language"    tests\test_user_prompt_roundtrip.py
call :runtest "9/27 Edge selection"           tests\test_edge_selection.py
call :runtest "10/27 Ollama auto-start"       tests\test_ollama_launch.py
call :runtest "11/27 Cancellation"            tests\test_cancel.py
call :runtest "12/27 AI timeout config"       tests\test_timeout_config.py
call :runtest "13/27 Create sketch"           tests\test_create_sketch.py
call :runtest "14/27 Sketch-extrude link"     tests\test_extrude_link.py
call :runtest "15/27 Move and rotate"         tests\test_transform.py
call :runtest "16/27 Id-chaining helpers"     tests\test_idchain.py
call :runtest "17/27 Id-chaining guard"       tests\test_idchain_guard.py
call :runtest "18/27 Mirror and array"        tests\test_duplicate.py
call :runtest "19/27 Sketch on face + pocket" tests\test_sketch_on_face.py
call :runtest "20/27 Topology flip"           tests\test_flip.py
call :runtest "21/27 Engine launcher"         tests\test_launcher.py
call :runtest "22/27 Agentic feature loop"    tests\test_feature_loop.py
call :runtest "23/27 Questions (ADR 0018)"    tests\test_questions.py
call :runtest "24/27 Model ask + memory"      tests\test_session_memory.py
call :runtest "25/27 Primitives + revolve"    tests\test_primitives.py
call :runtest "26/27 Vocab B (loft/sweep/shell)" tests\test_vocab_b.py
call :runtest "27/27 AI client + config layer"   tests\test_ai_client.py
call :runtest "28/28 Free-Python compat shim"    tests\test_free_python_compat.py
call :runtest "28/28 Free-Python compat shim"    tests\test_free_python_compat.py

echo  ============================================================
if "%FAIL%"=="0" (
  echo   RESULT: ALL TESTS PASSED.
  echo RESULT: ALL TESTS PASSED. >> "%LOG%"
) else (
  echo   RESULT: AT LEAST ONE TEST FAILED ^(see above^).
  echo RESULT: AT LEAST ONE TEST FAILED >> "%LOG%"
)
echo  ============================================================
echo.
echo  Press any key to close this window...
pause >nul
exit /b 0

:runtest
echo  [%~1] ...
echo ===== %~1 ===== >> "%LOG%"
%PYEXE% "%~dp0%~2" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo       FAILED  ^(details in tests_output.txt^)
  set "FAIL=1"
) else (
  echo       passed
)
echo. >> "%LOG%"
exit /b 0
