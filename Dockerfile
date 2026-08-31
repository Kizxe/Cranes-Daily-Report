# Playwright's own image ships Chromium + its OS deps preinstalled —
# skips the usual apt-get fight for headless-browser libraries.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend
COPY frontend/ ./frontend
COPY templates/ ./templates
COPY scripts/ ./scripts

RUN mkdir -p reports uploads/pdfs logs backups backend/data

ENV TZ=Asia/Kuala_Lumpur
EXPOSE 8000

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
