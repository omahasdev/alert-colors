@echo off
rem Double-click: menu for install, update, restart, status, uninstall, build, configurator, watched place and tokens.
rem With an argument: run that action, for example "setup.cmd status".
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage.ps1" %*
