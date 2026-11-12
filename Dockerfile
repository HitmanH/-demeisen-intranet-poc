# Lambda container image (Debian base so LibreOffice is available; AWS runtime client via awslambdaric)
FROM public.ecr.aws/docker/library/python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-dejavu fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ADD https://truststore.pki.rds.amazonaws.com/eu-west-2/eu-west-2-bundle.pem /app/rds-ca.pem
RUN chmod -R a+rX /app && python -m compileall -q /app
# Lambda: only /tmp is writable
ENV INTRANET_PROD=1 PYTHONUNBUFFERED=1 DB_SSL_CA=/app/rds-ca.pem HOME=/tmp
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["lambda_handler.handler"]
