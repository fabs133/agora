@ECHO OFF
pushd %~dp0

REM Sphinx Windows wrapper. Usage: make.bat html | clean | livehtml | linkcheck

if "%SPHINXBUILD%" == "" (
	set SPHINXBUILD=sphinx-build
)
set SOURCEDIR=.
set BUILDDIR=_build
set SPHINXOPTS=-W --keep-going

if "%1" == "" goto help
if "%1" == "clean" goto clean
if "%1" == "livehtml" goto livehtml

%SPHINXBUILD% -M %1 %SOURCEDIR% %BUILDDIR% %SPHINXOPTS% %O%
goto end

:help
%SPHINXBUILD% -M help %SOURCEDIR% %BUILDDIR% %SPHINXOPTS% %O%
goto end

:clean
if exist %BUILDDIR% rmdir /s /q %BUILDDIR%
if exist api\_autosummary rmdir /s /q api\_autosummary
goto end

:livehtml
sphinx-autobuild %SOURCEDIR% %BUILDDIR%\html %SPHINXOPTS% %O%
goto end

:end
popd
