#!/bin/bash
# Start Ollama in background
ollama serve &

# Wait for Ollama to be ready
sleep 5

# Pull model in background (don't block uvicorn)
ollama pull deepseek-llm &

# Start FastAPI in FOREGROUND (no & at the end)
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}