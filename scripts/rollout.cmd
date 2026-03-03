@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM =========================
REM MCT VM Rollout (VMware)
REM - Copies bunnyXX.vmdk.zst to remote PCs via \\PC\C$ share
REM - Verifies SHA256 on target
REM - Remote-unpacks using schtasks as SYSTEM
REM - Deletes .zst after unpack
REM - Writes marker bunnyXX.deployed.sha256 to enable idempotent re-runs
REM =========================

set "DEFAULT_CSV=%~dp0rollout.csv"
set "DEFAULT_SRC=%~dp0images"
set "DEFAULT_TOOLS=%~dp0tools"
set "DEFAULT_LOGDIR=%~dp0logs"
set "TARGET_DIR=C:\Virtual_Machines"
set "TARGET_TOOLS_DIR=C:\Virtual_Machines\tools"

set "CSV_FILE=%DEFAULT_CSV%"
set "SRC_DIR=%DEFAULT_SRC%"
set "TOOLS_DIR=%DEFAULT_TOOLS%"
set "LOGDIR=%DEFAULT_LOGDIR%"
set "ZSTD_EXE_LOCAL=%TOOLS_DIR%\zstd.exe"
set "RETRIES=2"
set "PING_TIMEOUT_MS=800"
set "WAIT_SECONDS=120"
set "DRY_RUN=0"
set "ONLY_PC="
set "FORCE=0"

set "LOGFILE=%LOGDIR%\rollout-%DATE:~-4%-%DATE:~3,2%-%DATE:~0,2%_%TIME:~0,2%-%TIME:~3,2%-%TIME:~6,2%.log"
set "LOGFILE=%LOGFILE: =0%"

call :parse_args %*
if errorlevel 1 exit /b 1

call :ensure_admin
if errorlevel 1 exit /b 1

if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

call :log "INFO" "=== MCT Rollout started ==="
call :log "INFO" "CSV     : %CSV_FILE%"
call :log "INFO" "SRC_DIR : %SRC_DIR%"
call :log "INFO" "TOOLS   : %TOOLS_DIR%"
call :log "INFO" "TARGET  : %TARGET_DIR%"
call :log "INFO" "RETRIES : %RETRIES%"
call :log "INFO" "DRY_RUN : %DRY_RUN%"
if defined ONLY_PC call :log "INFO" "ONLY_PC : %ONLY_PC%"
if "%FORCE%"=="1" call :log "INFO" "FORCE   : enabled (ignore marker, redeploy)"

if not exist "%CSV_FILE%" (
  call :log "ERROR" "CSV not found: %CSV_FILE%"
  exit /b 2
)
if not exist "%SRC_DIR%" (
  call :log "ERROR" "SRC_DIR not found: %SRC_DIR%"
  exit /b 2
)
if not exist "%ZSTD_EXE_LOCAL%" (
  call :log "ERROR" "Missing zstd.exe: %ZSTD_EXE_LOCAL%"
  exit /b 2
)

REM Process CSV
REM Expected columns (at least): pcname,vm,file,sha256
REM Additional columns are allowed and ignored.
REM Example:
REM pcname,vm,forgejo,name,email,file,sha256
REM S40404-01,bunny07,ploch,Leon Ploch,Leon.Ploch@sabel.education,bunny07.vmdk.zst,c3a4...

for /f "usebackq tokens=1-7 delims=," %%A in ("%CSV_FILE%") do (
  set "pc=%%~A"
  set "vm=%%~B"
  set "c6=%%~F"
  set "sha=%%~G"

  REM Skip header line
  if /i "!pc!"=="pcname" goto :continue_loop

  REM Allow CSV with only 4 cols: pc,vm,file,sha
  REM If tokens 6/7 are empty, fall back to tokens 3/4
  if "!c6!"=="" (
    set "file=%%~C"
    set "sha=%%~D"
  ) else (
    set "file=!c6!"
    set "sha=!sha!"
  )

  REM Optional filter
  if defined ONLY_PC (
    if /i not "!pc!"=="%ONLY_PC%" goto :continue_loop
  )

  if "!pc!"=="" goto :continue_loop
  if "!vm!"=="" (
    call :log "WARN" "Skipping row with empty vm for pc=!pc!"
    goto :continue_loop
  )
  if "!file!"=="" (
    call :log "WARN" "Skipping row with empty file for pc=!pc!, vm=!vm!"
    goto :continue_loop
  )
  if "!sha!"=="" (
    call :log "WARN" "Skipping row with empty sha256 for pc=!pc!, vm=!vm!"
    goto :continue_loop
  )

  call :deploy_one "!pc!" "!vm!" "!file!" "!sha!"
  if errorlevel 1 (
    call :log "ERROR" "Deploy FAILED for pc=!pc! vm=!vm!"
  ) else (
    call :log "INFO"  "Deploy OK/SKIP for pc=!pc! vm=!vm!"
  )

  :continue_loop
)

call :log "INFO" "=== MCT Rollout finished ==="
echo.
echo Log: "%LOGFILE%"
exit /b 0

REM -------------------------
REM Functions
REM -------------------------

:help
echo.
echo MCT VM Rollout for VMware (central, admin, remote)
echo.
echo Usage:
echo   rollout.cmd [--csv PATH] [--src PATH] [--tools PATH] [--logdir PATH] [--retries N]
echo              [--only PCNAME] [--force] [--dry-run] [--help]
echo.
echo What it does (per CSV row):
echo   1) Hostname check: ping PCNAME
echo   2) Ensure target dirs: \\PCNAME\C$\Virtual_Machines\ and \tools\
echo   3) Copy tools\zstd.exe to target (if missing or --force)
echo   4) Copy one file: bunnyXX.vmdk.zst to target
echo   5) Verify SHA256 of the .zst on the target (certutil over UNC)
echo      - If mismatch: delete and retry (up to --retries)
echo   6) Remote unpack (SYSTEM) via schtasks:
echo        zstd.exe -d -f bunnyXX.vmdk.zst -o bunnyXX.vmdk
echo      then delete bunnyXX.vmdk.zst
echo      then write marker bunnyXX.deployed.sha256
echo   7) Idempotent:
echo      - If marker exists and matches expected SHA256: SKIP (unless --force)
echo.
echo CSV format:
echo   Must contain at least the columns:
echo     pcname,vm,file,sha256
echo   You MAY also use the extended format (preferred):
echo     pcname,vm,forgejo,name,email,file,sha256
echo.
echo Examples:
echo   rollout.cmd --src D:\rollout\images --tools D:\rollout\tools --csv D:\rollout\rollout.csv
echo   rollout.cmd --only S40404-01 --src D:\rollout\images
echo   rollout.cmd --force --src D:\rollout\images
echo.
echo Notes:
echo   - Must run with admin rights (UAC prompt will appear if needed).
echo   - Requires access to \\PC\C$ and Remote Scheduled Tasks.
echo   - Target folder is fixed: C:\Virtual_Machines\
echo.
exit /b 0

:parse_args
:parse_args_loop
if "%~1"=="" exit /b 0
if /i "%~1"=="--help"  call :help & exit /b 1
if /i "%~1"=="-h"      call :help & exit /b 1

if /i "%~1"=="--csv" (
  set "CSV_FILE=%~2"
  shift & shift
  goto :parse_args_loop
)
if /i "%~1"=="--src" (
  set "SRC_DIR=%~2"
  shift & shift
  goto :parse_args_loop
)
if /i "%~1"=="--tools" (
  set "TOOLS_DIR=%~2"
  set "ZSTD_EXE_LOCAL=%~2\zstd.exe"
  shift & shift
  goto :parse_args_loop
)
if /i "%~1"=="--logdir" (
  set "LOGDIR=%~2"
  set "LOGFILE=%LOGDIR%\rollout-%DATE:~-4%-%DATE:~3,2%-%DATE:~0,2%_%TIME:~0,2%-%TIME:~3,2%-%TIME:~6,2%.log"
  set "LOGFILE=%LOGFILE: =0%"
  shift & shift
  goto :parse_args_loop
)
if /i "%~1"=="--retries" (
  set "RETRIES=%~2"
  shift & shift
  goto :parse_args_loop
)
if /i "%~1"=="--only" (
  set "ONLY_PC=%~2"
  shift & shift
  goto :parse_args_loop
)
if /i "%~1"=="--dry-run" (
  set "DRY_RUN=1"
  shift
  goto :parse_args_loop
)
if /i "%~1"=="--force" (
  set "FORCE=1"
  shift
  goto :parse_args_loop
)

echo ERROR: Unknown option: %~1
echo Use --help
exit /b 1

:ensure_admin
REM Check admin by attempting a privileged operation
net session >nul 2>&1
if %errorlevel%==0 exit /b 0

echo.
echo This script needs Administrator privileges.
echo A UAC prompt will appear. Please confirm.
echo.

REM Relaunch self elevated
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs" >nul 2>&1
exit /b 1

:deploy_one
set "PC=%~1"
set "VM=%~2"
set "FILE=%~3"
set "EXP_SHA=%~4"

set "UNC_ROOT=\\%PC%\C$"
set "UNC_TARGET=%UNC_ROOT%\Virtual_Machines"
set "UNC_TOOLS=%UNC_TARGET%\tools"
set "UNC_ZST=%UNC_TARGET%\%FILE%"
set "UNC_VMDK=%UNC_TARGET%\%VM%.vmdk"
set "UNC_MARKER=%UNC_TARGET%\%VM%.deployed.sha256"

set "LOCAL_ZST=%SRC_DIR%\%FILE%"

call :log "INFO" "---- pc=%PC% vm=%VM% file=%FILE% ----"

REM Hostname / reachability check
call :log "INFO" "Ping %PC% ..."
ping -n 1 -w %PING_TIMEOUT_MS% "%PC%" >nul 2>&1
if errorlevel 1 (
  call :log "WARN" "Host unreachable (ping failed): %PC%  -> SKIP"
  exit /b 0
)

REM Check marker (idempotent)
if "%FORCE%"=="0" (
  if exist "%UNC_MARKER%" (
    for /f "usebackq delims=" %%M in ("%UNC_MARKER%") do set "MARKER_SHA=%%~M"
    if /i "!MARKER_SHA!"=="%EXP_SHA%" (
      call :log "INFO" "Marker present and matches expected SHA256 -> SKIP"
      exit /b 0
    ) else (
      call :log "WARN" "Marker exists but hash differs -> will redeploy"
    )
  )
)

REM Ensure target dirs
call :log "INFO" "Ensure target dirs on %PC% ..."
if "%DRY_RUN%"=="1" (
  call :log "INFO" "[dry-run] mkdir %UNC_TARGET% and %UNC_TOOLS%"
) else (
  mkdir "%UNC_TARGET%" >nul 2>&1
  mkdir "%UNC_TOOLS%"  >nul 2>&1
)

REM Copy zstd.exe (always if force, else if missing)
if "%FORCE%"=="1" (
  call :copy_file "%ZSTD_EXE_LOCAL%" "%UNC_TOOLS%\zstd.exe"
) else (
  if not exist "%UNC_TOOLS%\zstd.exe" (
    call :copy_file "%ZSTD_EXE_LOCAL%" "%UNC_TOOLS%\zstd.exe"
  ) else (
    call :log "INFO" "zstd.exe already present on target"
  )
)

REM If a previous vmdk exists (old version), remove it to avoid confusion
if exist "%UNC_VMDK%" (
  call :log "WARN" "Old VMDK exists on target -> deleting %UNC_VMDK%"
  if "%DRY_RUN%"=="1" (
    call :log "INFO" "[dry-run] del /f /q %UNC_VMDK%"
  ) else (
    del /f /q "%UNC_VMDK%" >nul 2>&1
  )
)

REM Copy + verify loop
set /a "try=0"
:copy_try
set /a "try+=1"
call :log "INFO" "Copy try !try!/%RETRIES%: %LOCAL_ZST% -> %UNC_TARGET%"

if not exist "%LOCAL_ZST%" (
  call :log "ERROR" "Source file missing: %LOCAL_ZST%"
  exit /b 1
)

if "%DRY_RUN%"=="1" (
  call :log "INFO" "[dry-run] robocopy ""%SRC_DIR%"" ""%UNC_TARGET%"" ""%FILE%"""
) else (
  robocopy "%SRC_DIR%" "%UNC_TARGET%" "%FILE%" /R:2 /W:2 /NFL /NDL /NP /NJH /NJS >nul
  REM robocopy returns codes; 0-7 are generally OK (incl. skipped/copy)
  if errorlevel 8 (
    call :log "ERROR" "robocopy failed with code %errorlevel%"
    exit /b 1
  )
)

REM Verify SHA256 on target (UNC)
call :log "INFO" "Verify SHA256 on target: %UNC_ZST%"
call :get_sha256 "%UNC_ZST%" GOT_SHA
if errorlevel 1 (
  call :log "ERROR" "Could not compute SHA256 for %UNC_ZST%"
  if "!try!" LSS %RETRIES% goto :copy_try
  exit /b 1
)

if /i not "!GOT_SHA!"=="%EXP_SHA%" (
  call :log "ERROR" "SHA256 mismatch on target!"
  call :log "ERROR" "Expected: %EXP_SHA%"
  call :log "ERROR" "Got     : !GOT_SHA!"
  call :log "WARN"  "Deleting bad file and retrying..."
  if "%DRY_RUN%"=="1" (
    call :log "INFO" "[dry-run] del /f /q %UNC_ZST%"
  ) else (
    del /f /q "%UNC_ZST%" >nul 2>&1
  )
  if "!try!" LSS %RETRIES% goto :copy_try
  exit /b 1
)

call :log "INFO" "SHA256 OK"

REM Remote unpack + delete zst + write marker using schtasks (SYSTEM)
call :log "INFO" "Remote unpack via schtasks (SYSTEM) on %PC% ..."
call :remote_unpack "%PC%" "%VM%" "%FILE%" "%EXP_SHA%"
if errorlevel 1 (
  call :log "ERROR" "Remote unpack failed on %PC%"
  exit /b 1
)

REM Wait for result: vmdk exists AND zst deleted AND marker written
call :log "INFO" "Waiting up to %WAIT_SECONDS%s for unpack completion..."
set /a "elapsed=0"
:wait_loop
if exist "%UNC_VMDK%" (
  if not exist "%UNC_ZST%" (
    if exist "%UNC_MARKER%" (
      call :log "INFO" "Unpack done: VMDK present, ZST removed, marker present."
      exit /b 0
    )
  )
)

if "%DRY_RUN%"=="1" (
  call :log "INFO" "[dry-run] would wait/poll here"
  exit /b 0
)

timeout /t 2 /nobreak >nul
set /a "elapsed+=2"
if !elapsed! GEQ %WAIT_SECONDS% (
  call :log "ERROR" "Timeout waiting for unpack completion on %PC%"
  exit /b 1
)
goto :wait_loop

:copy_file
set "SRC=%~1"
set "DST=%~2"
call :log "INFO" "Copy %SRC% -> %DST%"
if "%DRY_RUN%"=="1" (
  call :log "INFO" "[dry-run] copy /y ""%SRC%"" ""%DST%"""
  exit /b 0
)
copy /y "%SRC%" "%DST%" >nul
if errorlevel 1 (
  call :log "ERROR" "Copy failed: %SRC% -> %DST%"
  exit /b 1
)
exit /b 0

:get_sha256
REM usage: call :get_sha256 "path" OUTVAR
set "P=%~1"
set "OUTVAR=%~2"
set "HASH="
for /f "tokens=* delims=" %%H in ('certutil -hashfile "%P%" SHA256 ^| findstr /R /I "^[0-9a-f][0-9a-f]*$"') do (
  set "HASH=%%H"
  goto :sha_done
)
:sha_done
if not defined HASH exit /b 1
set "%OUTVAR%=%HASH%"
exit /b 0

:remote_unpack
REM Create + run a scheduled task on remote PC as SYSTEM:
REM  - zstd -d -f C:\Virtual_Machines\file.zst -o C:\Virtual_Machines\vm.vmdk
REM  - delete zst
REM  - write marker
set "RPC=%~1"
set "RVM=%~2"
set "RFILE=%~3"
set "RSHA=%~4"
set "TASK=MCT_Unpack_%RVM%"

set "CMDLINE=cmd.exe /c ""^
""%TARGET_TOOLS_DIR%\zstd.exe"" -d -f ""%TARGET_DIR%\%RFILE%"" -o ""%TARGET_DIR%\%RVM%.vmdk"" ^&^& ^
del /f /q ""%TARGET_DIR%\%RFILE%"" ^&^& ^
echo %RSHA%> ""%TARGET_DIR%\%RVM%.deployed.sha256"" ^&^& ^
exit /b 0"""

call :log "INFO" "schtasks create/run/delete on %RPC% task=%TASK%"
if "%DRY_RUN%"=="1" (
  call :log "INFO" "[dry-run] schtasks /Create /S %RPC% ... /TN %TASK% /TR <cmdline>"
  call :log "INFO" "[dry-run] schtasks /Run    /S %RPC% /TN %TASK%"
  call :log "INFO" "[dry-run] schtasks /Delete /S %RPC% /TN %TASK% /F"
  exit /b 0
)

schtasks /Create /S "%RPC%" /TN "%TASK%" /TR "%CMDLINE%" /SC ONCE /ST 00:00 /SD 01/01/2099 /RU SYSTEM /RL HIGHEST /F >nul 2>&1
if errorlevel 1 (
  call :log "ERROR" "schtasks /Create failed on %RPC%"
  exit /b 1
)

schtasks /Run /S "%RPC%" /TN "%TASK%" >nul 2>&1
if errorlevel 1 (
  call :log "ERROR" "schtasks /Run failed on %RPC%"
  schtasks /Delete /S "%RPC%" /TN "%TASK%" /F >nul 2>&1
  exit /b 1
)

REM Cleanup task definition (job keeps running even after delete)
schtasks /Delete /S "%RPC%" /TN "%TASK%" /F >nul 2>&1
exit /b 0

:log
set "LVL=%~1"
set "MSG=%~2"
echo [%DATE% %TIME%] %LVL% %MSG%
>>"%LOGFILE%" echo [%DATE% %TIME%] %LVL% %MSG%
exit /b 0
