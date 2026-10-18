FROM mcr.microsoft.com/playwright/python:v1.64.0-noble

WORKDIR /app

RUN pip install --no-cache-dir "playwright==1.64.0" "httpx==0.28.1"

COPY collector_cloud/collector.py /app/collector.py

CMD ["python", "/app/collector.py"]
