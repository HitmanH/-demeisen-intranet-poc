FROM public.ecr.aws/docker/library/python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-dejavu fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd -m app && chown -R app /app && chmod 644 /app/rds-ca.pem
USER app
ENV INTRANET_PROD=1 PYTHONUNBUFFERED=1 DB_SSL_CA=/app/rds-ca.pem
EXPOSE 8080
CMD ["gunicorn", "--preload", "-w", "2", "--threads", "4", "-t", "300", "-b", "0.0.0.0:8080", "app:app"]
