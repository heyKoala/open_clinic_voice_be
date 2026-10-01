@echo off
echo Starting Celery Beat Scheduler...
if exist .venv\Scripts\activate.bat (call .venv\Scripts\activate.bat) else (call venv\Scripts\activate.bat 2>nul)
if not exist .celery mkdir .celery
where watchmedo >nul 2>nul
if %errorlevel%==0 (
    watchmedo auto-restart --directory=./ --pattern=*.py --ignore-patterns="*/.venv/*;*/venv/*;*/media/*;*/.celery/*" --recursive -- python -m celery -A config beat -l INFO
) else (
    echo watchmedo not found ^(pip install watchdog for auto-reload^); starting without auto-reload.
    python -m celery -A config beat -l INFO
)
