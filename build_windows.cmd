@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo [1/5] 檢查 Python...
py -3.12 --version >nul 2>&1
if errorlevel 1 (
  echo 找不到 Python 3.12，請先安裝 64 位元 Python 3.12。
  pause
  exit /b 1
)

echo [2/5] 建立封裝環境...
if not exist ".venv_build\Scripts\python.exe" py -3.12 -m venv .venv_build
if errorlevel 1 goto :error

echo [3/5] 安裝固定版本相依套件...
call ".venv_build\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
call ".venv_build\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto :error
if not exist "assets\NotoSansTC-Regular.ttf" (
  where npm >nul 2>&1
  if errorlevel 1 (
    echo 找不到 npm，無法準備內建中文字型。
    goto :error
  )
  call npm install --no-save --ignore-scripts @fontsource/noto-sans-tc@5.2.9
  if errorlevel 1 goto :error
  call ".venv_build\Scripts\python.exe" prepare_font.py
  if errorlevel 1 goto :error
)

echo [4/5] 建立 Windows 免安裝版...
if exist "build" rmdir /s /q "build"
if exist "dist\AON_XML_to_DXF" rmdir /s /q "dist\AON_XML_to_DXF"
call ".venv_build\Scripts\python.exe" -m PyInstaller --noconfirm AON_XML_to_DXF.spec
if errorlevel 1 goto :error
copy /y "README_使用說明.txt" "dist\AON_XML_to_DXF\README_使用說明.txt" >nul
copy /y "THIRD_PARTY_NOTICES.txt" "dist\AON_XML_to_DXF\THIRD_PARTY_NOTICES.txt" >nul

echo [5/5] 建立 ZIP...
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path 'dist\AON_XML_to_DXF_Windows64.zip') { Remove-Item 'dist\AON_XML_to_DXF_Windows64.zip' }; Compress-Archive -Path 'dist\AON_XML_to_DXF\*' -DestinationPath 'dist\AON_XML_to_DXF_Windows64.zip'"
if errorlevel 1 goto :error

echo.
echo 完成：dist\AON_XML_to_DXF_Windows64.zip
echo 主程式：dist\AON_XML_to_DXF\AON_XML_to_DXF.exe
pause
exit /b 0

:error
echo.
echo 封裝失敗，請保留本視窗內容以便檢查。
pause
exit /b 1
