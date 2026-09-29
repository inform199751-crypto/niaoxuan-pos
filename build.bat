@echo off
rem 這個檔案是 UTF-8,先切換主控台字碼頁,中文檔名才不會變亂碼
chcp 65001 >nul
rem 把 app\ 打包成單一 exe(店家電腦不用裝 Python)
rem 需要:Python 3.10 以上,以及 pip install pyinstaller
cd /d "%~dp0app"
python -m PyInstaller --onefile --console --name 鳥璇點餐系統 ^
  --add-data "index.html;." --add-data "guest.html;." ^
  --distpath ..\dist --workpath ..\build --specpath ..\build -y server.py
echo.
echo 完成:dist\鳥璇點餐系統.exe
pause
