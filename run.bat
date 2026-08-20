@echo off
rem ---------------------------------------------------------------------------
rem  PithLocatorCT launcher.
rem
rem  Drag a folder of RingIndicator output onto this file, or double-click it and
rem  type the path when asked.
rem
rem  Three stages, in this order, because the order is what makes the failures
rem  readable: find a Python that actually runs, offer to install one if there
rem  is none, and only then worry about packages. Announcing a package install
rem  before knowing whether Python exists is what produced the original
rem  "'python' is not recognized" confusion.
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "HERE=%~dp0"
title PithLocatorCT

echo.
echo  PithLocatorCT - pith offset estimator
echo  ------------------------------------
echo.

rem ------------------------------------------------------------------ folder
set "FOLDER=%~1"
if not defined FOLDER set /p "FOLDER=Folder with the cores: "
if defined FOLDER set FOLDER=!FOLDER:"=!
if not defined FOLDER (
    echo  No folder given.
    goto :end
)
if not exist "!FOLDER!\" (
    echo  Not a folder: !FOLDER!
    goto :end
)

rem --------------------------------------------------------- stage 1: python
call :find_python
if not defined PY (
    echo  No working Python 3 was found on this machine.
    echo.
    set "ANSWER=Y"
    set /p "ANSWER=Install Python now? [Y/n] "
    if /i "!ANSWER!"=="n"  goto :no_python
    if /i "!ANSWER!"=="no" goto :no_python
    call :install_python
    if errorlevel 1 goto :no_python
    call :find_python
    if not defined PY (
        echo.
        echo  Python was installed but still cannot be found. Close this window,
        echo  open a new one, and run this file again.
        goto :end
    )
)
echo  Python:  !PY!

rem ------------------------------------------------------- stage 3: packages
!PY! -c "import numpy, tifffile, PIL, openpyxl" >nul 2>nul
if errorlevel 1 (
    call :install_packages
    if errorlevel 1 goto :end
)

rem -------------------------------------------------------------------- run
echo  Folder:  !FOLDER!
echo.
!PY! pithlocator.py "!FOLDER!"
goto :end


rem ===========================================================================
rem  Look for a usable interpreter, most specific location first. Every
rem  candidate is validated by RUNNING it, never by checking that the file
rem  exists: %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe is on PATH on many
rem  Windows machines and is only a stub that opens the Microsoft Store, so an
rem  existence test finds a "Python" that cannot execute anything.
rem ===========================================================================
:find_python
set "PY="

rem a portable Python unzipped next to this file wins over everything else
if exist "!HERE!python\python.exe" (
    call :validate "!HERE!python\python.exe"
    if defined PY goto :eof
)

rem the official launcher: present whenever python.org's installer was used,
rem and it keeps working when PATH does not
call :validate py -3
if defined PY goto :eof

rem ordinary PATH installs
call :validate python
if defined PY goto :eof
call :validate python3
if defined PY goto :eof

rem per-user install, which is also where winget puts it. Newest first.
call :scan_dir "%LOCALAPPDATA%\Programs\Python"
if defined PY goto :eof

rem all-users installs
call :scan_dir "%ProgramFiles%"
if defined PY goto :eof
call :scan_dir "%ProgramFiles(x86)%"
if defined PY goto :eof
call :scan_dir "C:"
if defined PY goto :eof

rem the registry is the authoritative record and catches anything above missed
call :scan_registry HKCU
if defined PY goto :eof
call :scan_registry HKLM
if defined PY goto :eof

rem a real Store install works even though the stub does not
if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" (
    call :validate "%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
)
goto :eof


rem --- Python3* subfolders of one parent directory, newest name first --------
:scan_dir
set "PARENT=%~1"
if not exist "!PARENT!\" goto :eof
for /f "delims=" %%D in ('dir /b /a:d /o-n "!PARENT!\Python3*" 2^>nul') do (
    if exist "!PARENT!\%%D\python.exe" (
        call :validate "!PARENT!\%%D\python.exe"
        if defined PY goto :eof
    )
)
goto :eof


rem --- ExecutablePath values recorded under one registry hive ----------------
:scan_registry
for /f "tokens=2,*" %%A in ('reg query "%1\Software\Python\PythonCore" /s /v ExecutablePath 2^>nul') do (
    if exist "%%B" (
        call :validate "%%B"
        if defined PY goto :eof
    )
)
goto :eof


rem --- run a candidate; set PY only if it executed Python successfully -------
:validate
%* -c "import sys" >nul 2>nul
if errorlevel 1 goto :eof
set "PY=%*"
goto :eof


rem ===========================================================================
:install_python
where winget >nul 2>nul
if errorlevel 1 (
    echo.
    echo  winget is not available on this machine, so Python cannot be
    echo  installed automatically.
    exit /b 1
)
echo.
echo  Installing Python 3.12 with winget. This takes a minute or two and
echo  does not need administrator rights.
echo.
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
exit /b 0


rem ===========================================================================
:install_packages
echo.
echo  Installing the Python packages this tool needs:
echo     numpy, tifffile, pillow, openpyxl
echo.
!PY! -m pip install --user -r requirements.txt
if not errorlevel 1 exit /b 0
echo.
echo  That failed; retrying without --user.
!PY! -m pip install -r requirements.txt
if not errorlevel 1 exit /b 0
echo.
echo  The packages could not be installed automatically. Run this by hand:
echo.
echo     !PY! -m pip install numpy tifffile pillow openpyxl
echo.
exit /b 1


rem ===========================================================================
:no_python
echo.
echo  Python 3.8 or newer is required. Three ways to get it:
echo.
echo    1. In a terminal:  winget install -e --id Python.Python.3.12
echo.
echo    2. https://www.python.org/downloads/windows/
echo       During setup, tick "Add python.exe to PATH".
echo.
echo    3. Offline machine: unzip a portable Python into a folder named
echo       "python" next to this file, so that python\python.exe exists.
echo.
echo  Then run this file again.
goto :end


rem ===========================================================================
:end
echo.
pause
endlocal
