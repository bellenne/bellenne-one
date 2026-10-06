FROM python:3.12-slim AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY apps/parity/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
FROM dependencies AS base
COPY apps/parity/app ./app
COPY apps/parity/extension ./extension
COPY apps/pulse/app/static/css/theme.css ./app/static/theme.css
COPY apps/pulse/app/static/css/app.css ./app/static/app.css
COPY apps/pulse/app/static/js/app.js ./app/static/app.js
RUN python extension/build.py

FROM base AS test
COPY apps/parity/tests ./tests
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]

FROM dependencies AS browser-dependencies
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps chromium && chmod -R a+rX /ms-playwright

FROM browser-dependencies AS browser
COPY --from=base /app/app ./app

FROM browser AS browser-test
COPY --from=base /app/extension ./extension
COPY apps/parity/tests ./tests
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]

FROM browser AS worker
RUN addgroup --system bellenneparity && adduser --system --ingroup bellenneparity --home /app bellenneparity && mkdir -p /data && chown -R bellenneparity:bellenneparity /app /data
USER bellenneparity
CMD ["python", "-m", "app.worker"]

FROM base AS runtime
RUN addgroup --system bellenneparity && adduser --system --ingroup bellenneparity --home /app bellenneparity && mkdir -p /data && chown -R bellenneparity:bellenneparity /app /data
USER bellenneparity
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
