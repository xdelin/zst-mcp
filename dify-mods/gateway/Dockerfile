FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.115.* uvicorn==0.32.* docker==7.1.* httpx==0.28.*
COPY gateway.py /app/gateway.py
WORKDIR /app
EXPOSE 8100
CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8100"]
