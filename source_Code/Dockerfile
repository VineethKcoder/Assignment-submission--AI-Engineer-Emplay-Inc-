FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# CPU torch wheel keeps the image small; use an nvidia/cuda base + cu121 wheel for GPU.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch && pip install -r requirements.txt

COPY . .
EXPOSE 8000 8501
CMD ["python", "main.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
