@echo off
REM Inicia el servidor y abre el navegador en http://localhost:8000
cd /d "%~dp0"
echo Revisando las librerias (la primera vez tarda un poco)...
python -m pip install -q -r requirements.txt
if errorlevel 1 goto error
echo.
echo Servidor iniciando. NO CIERRES ESTA VENTANA mientras uses la app.
echo Si el navegador no abre solo, entra a http://localhost:8000
start "" cmd /c "timeout /t 4 >nul & start http://localhost:8000"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo El servidor se detuvo. Revisa el mensaje de arriba.
pause
exit /b
:error
echo.
echo No se pudieron instalar las librerias. Verifica que Python este instalado
echo (descargalo de python.org y marca "Add Python to PATH") y que tengas internet.
pause
