#!/bin/bash

# Start FastAPI in FOREGROUND (no & at the end)
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}