FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY apps/nest/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/nest/app ./app
COPY apps/pulse/app/static/css/theme.css ./app/static/theme.css
COPY apps/pulse/app/static/css/app.css ./app/static/app.css
COPY apps/pulse/app/static/js/app.js ./app/static/app.js

FROM base AS test
COPY apps/nest/requirements-dev.txt ./requirements-dev.txt
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY apps/nest/tests ./tests
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime
RUN addgroup --system bellennenest \
    && adduser --system --ingroup bellennenest --home /app bellennenest \
    && mkdir -p /data \
    && chown -R bellennenest:bellennenest /app /data
USER bellennenest
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
