FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY schedules.yaml .

# Override at runtime if needed
ENV SCHEDULER_CONFIG=/app/schedules.yaml
ENV SCREENER_URL=http://strategy-screener:8000
ENV TRADING_URL=http://trading-service:8001

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
