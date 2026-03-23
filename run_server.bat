@echo off
setlocal enabledelayedexpansion

echo.
echo ==========================================
echo    Parasite Server Bootstrapper
echo ==========================================
echo.

:: 1. Install Dependencies
echo [1/4] Installing dependencies from requirements.txt...
python -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Failed to install dependencies. 
    echo Please ensure Python and Pip are installed and in your PATH.
    pause
    exit /b %ERRORLEVEL%
)

:: 2. Handle API Key
echo [2/4] Checking Steam API Key...
if not exist apikey.txt (
    echo apikey.txt not found.
    set /p USER_KEY="Please enter your Steam API Key: "
    echo !USER_KEY! > apikey.txt
    set "API_KEY=!USER_KEY!"
) else (
    :: Read API Key from file
    for /f "usebackq delims=" %%A in ("apikey.txt") do set "API_KEY=%%A"
)

:: Trim spaces (basic)
set "API_KEY=%API_KEY: =%"

:: 3. Update .env
echo [3/4] Updating .env configuration...
if not exist .env (
    echo Creating new .env file...
    (
        echo STEAM_API_KEY=%API_KEY%
        echo STEAM_APP_ID=999860
    ) > .env
) else (
    echo Updating existing .env file...
    set "FOUND_KEY=0"
    (for /f "usebackq tokens=1* delims==" %%A in (".env") do (
        if /I "%%A"=="STEAM_API_KEY" (
            echo STEAM_API_KEY=%API_KEY%
            set "FOUND_KEY=1"
        ) else (
            if not "%%B"=="" (
                echo %%A=%%B
            ) else (
                echo %%A
            )
        )
    )) > .env.new
    
    if "!FOUND_KEY!"=="0" (
        echo STEAM_API_KEY=%API_KEY% >> .env.new
    )
    
    move /y .env.new .env >nul
)

:: 4. Launch Server
echo [4/4] Launching Parasite Python Server...
echo.
python main.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Server exited with code %ERRORLEVEL%.
    pause
)

pause
