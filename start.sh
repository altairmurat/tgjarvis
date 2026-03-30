#!/bin/bash
# Start Ollama in background
ollama serve &

# Wait for Ollama to be ready
sleep 5

# Pull the model
ollama pull deepseek-llm

# Start FastAPI
uvicorn main:app --host 0.0.0.0 --port 8000