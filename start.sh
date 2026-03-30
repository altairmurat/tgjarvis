#!/bin/bash
ollama serve &
sleep 5
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} &
ollama pull deepseek-llm &
wait