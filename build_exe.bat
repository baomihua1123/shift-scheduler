@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 安裝所需套件（需要網路，只需做一次）...
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail
echo [2/2] 打包成免安裝程式...
pyinstaller --noconfirm --noconsole --onedir --name ShiftScheduler main.py
if errorlevel 1 goto fail
echo.
echo 完成！請把整個資料夾  dist\ShiftScheduler  複製給使用者，雙擊其中的 ShiftScheduler.exe 即可執行。
echo 資料會存放在該資料夾內的 data 子資料夾（可直接整個資料夾備份或搬移）。
pause
exit /b 0
:fail
echo 發生錯誤，請把上面的訊息提供給我。
pause
exit /b 1
