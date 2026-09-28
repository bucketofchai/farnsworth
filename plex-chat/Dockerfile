FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py bridge.py ./
COPY static ./static
EXPOSE 7680
CMD ["python", "-u", "/app/app.py"]
