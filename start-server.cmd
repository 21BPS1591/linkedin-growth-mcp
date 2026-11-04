@echo off
cd /d "%~dp0"
uv run -m linkedin_growth_mcp --transport http --host 127.0.0.1 --port 8000 >> data\server.log 2>&1
