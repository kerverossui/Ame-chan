@echo off
rem Opens the Ame launcher (progress bars + LAUNCH button). Language: .installer_lang
rem   start.bat          -> normal, no console
rem   start.bat console  -> same, with a console window (troubleshooting)
rem If Python 3.10+ is missing it is installed for this user only (no admin,
rem no "Add to PATH" needed): winget first, then the official python.org
rem installer. Python is found through the py launcher or its install folder,
rem so PATH is never required.
setlocal EnableExtensions
cd /d "%~dp0"

set "AL=en"
if exist .installer_lang set /p AL=<.installer_lang
set "TRIED_INSTALL="

:find
call :findpython
if defined PYEXE goto havepython
if defined TRIED_INSTALL goto nopython
set "TRIED_INSTALL=1"
call :msg_installing
call :installpython
goto find

:havepython
"%PYEXE%" -c "import tkinter" >NUL 2>NUL
if errorlevel 1 goto notk
set "PYWEXE=%PYEXE:python.exe=pythonw.exe%"
if not exist "%PYWEXE%" set "PYWEXE=%PYEXE%"
if /i "%~1"=="console" goto console
if /i "%~1"=="consola" goto console
start "" "%PYWEXE%" ame_launcher.py
goto :eof

:console
"%PYEXE%" ame_launcher.py
pause
goto :eof

rem -- Find a usable Python 3.10+ and put its python.exe in PYEXE ----------
:findpython
set "PYEXE="
where py >NUL 2>NUL
if errorlevel 1 goto fp_where
for /f "usebackq delims=" %%P in (`py -3 -c "import sys;print(sys.executable)" 2^>NUL`) do set "PYEXE=%%P"
if defined PYEXE call :checkversion
if defined PYEXE exit /b 0
:fp_where
for /f "delims=" %%P in ('where python 2^>NUL ^| findstr /v /i "WindowsApps"') do if not defined PYEXE set "PYEXE=%%P"
if defined PYEXE call :checkversion
if defined PYEXE exit /b 0
:fp_local
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if not defined PYEXE if exist "%%D\python.exe" (
        set "PYEXE=%%D\python.exe"
        call :checkversion
    )
)
if defined PYEXE exit /b 0
for /d %%D in ("%ProgramFiles%\Python3*") do (
    if not defined PYEXE if exist "%%D\python.exe" (
        set "PYEXE=%%D\python.exe"
        call :checkversion
    )
)
exit /b 0

:checkversion
"%PYEXE%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >NUL 2>NUL
if errorlevel 1 set "PYEXE="
exit /b 0

rem -- Install Python 3.12 for the current user ----------------------------
:installpython
where winget >NUL 2>NUL
if errorlevel 1 goto ip_download
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :findpython
if defined PYEXE exit /b 0
:ip_download
set "PYINST=%TEMP%\python-3.12.10-amd64.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile '%PYINST%'"
if not exist "%PYINST%" exit /b 1
"%PYINST%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1 Include_test=0
del "%PYINST%" >NUL 2>NUL
exit /b 0

rem -- Messages -------------------------------------------------------------
:msg_installing
echo.
if /i "%AL%"=="es" goto mi_es
if /i "%AL%"=="it" goto mi_it
echo   Python was not found. Installing Python 3.12 for this user...
echo   This takes a minute or two. You don't need to do anything.
goto mi_end
:mi_es
echo   No encontre Python. Instalando Python 3.12 para este usuario...
echo   Tarda un minuto o dos. No tienes que hacer nada.
goto mi_end
:mi_it
echo   Python non trovato. Installo Python 3.12 per questo utente...
echo   Ci vuole un minuto o due. Non devi fare nulla.
:mi_end
echo.
exit /b 0

:nopython
echo.
if /i "%AL%"=="es" goto np_es
if /i "%AL%"=="it" goto np_it
echo   Python could not be installed automatically.
echo   Download it from https://www.python.org/downloads/
echo   open the installer and click "Install Now" - nothing else to tick.
echo   Then open start.bat again.
goto np_end
:np_es
echo   No se pudo instalar Python automaticamente.
echo   Descargalo de https://www.python.org/downloads/
echo   abre el instalador y haz clic en "Install Now" - no hay que marcar nada mas.
echo   Luego vuelve a abrir start.bat.
goto np_end
:np_it
echo   Non e stato possibile installare Python automaticamente.
echo   Scaricalo da https://www.python.org/downloads/
echo   apri l'installer e fai clic su "Install Now" - non serve spuntare altro.
echo   Poi riapri start.bat.
:np_end
echo.
start "" https://www.python.org/downloads/
pause
goto :eof

:notk
echo.
if /i "%AL%"=="es" goto nt_es
if /i "%AL%"=="it" goto nt_it
echo   Your Python doesn't include Tkinter, the window toolkit.
echo   Reinstall Python from python.org keeping "tcl/tk and IDLE" ticked.
goto nt_end
:nt_es
echo   Tu Python no trae Tkinter, la parte de ventanas.
echo   Reinstala Python desde python.org dejando marcada la opcion "tcl/tk and IDLE".
goto nt_end
:nt_it
echo   Il tuo Python non include Tkinter, la parte delle finestre.
echo   Reinstalla Python da python.org lasciando spuntata l'opzione "tcl/tk and IDLE".
:nt_end
echo.
pause
