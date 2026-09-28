FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py bridge.py ./
COPY static ./static
EXPOSE 7680
CMD ["sh", "-c", "exec python -u -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-7680}"]
