FROM python:3.10-slim

WORKDIR /app

# Копируем requirements
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код backend
COPY backend/ .

# Копируем frontend (важно!)
COPY frontend/ ./frontend

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]