# Образ для расчёта маршрута и Streamlit-приложения.
# База — тот же Python, на котором проект проверялся локально.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# Сначала зависимости: слой кэшируется и не пересобирается при правке кода
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем код и данные
COPY *.py ./
COPY route_with_losses.csv ./

# Приложение слушает 8501; health-эндпоинт Streamlit — /_stcore/health
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health')"

# По умолчанию — приложение.
# Восстановление маршрута: docker run --rm car-route python restore_route.py
CMD ["python", "-m", "streamlit", "run", "app.py"]
