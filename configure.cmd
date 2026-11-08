@echo off
rem Opens the configurator of the installed app in the browser (no admin rights).
rem Apply takes effect at once: the running app reloads the tuning file.
set "APP=%ProgramFiles%\AlertColors\app\AlertColors.exe"
set "CONFIG=%ProgramFiles%\AlertColors\config.toml"
if not exist "%APP%" (
  echo AlertColors is not installed. Run setup.cmd and choose Install.
  pause
  exit /b 1
)
start "" "%APP%" --config "%CONFIG%" tune
