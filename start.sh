#!/bin/bash
# Start Ollama in background
ollama serve &

# Wait for Ollama to be ready
sleep 5

# Start FastAPI first so Render sees the port
uvicorn main:app --host 0.0.0.0 --port 8000 &

# Pull the model in background while app is already running
ollama pull deepseek-llm

# Keep container alive
wait