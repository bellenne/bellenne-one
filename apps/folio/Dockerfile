FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FOLIO_DATA_DIR=/data
WORKDIR /app
COPY apps/folio/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN addgroup --system bellenne && adduser --system --ingroup bellenne --home /app bellenne
COPY apps/folio/app ./app
COPY apps/pulse/app/static/css/theme.css ./app/shared/css/theme.css
COPY apps/pulse/app/static/css/app.css ./app/shared/css/app.css
COPY apps/pulse/app/static/js/app.js ./app/shared/js/app.js
COPY apps/pulse/app/templates/icons.html ./app/templates/icons.html
RUN mkdir -p /data && chown -R bellenne:bellenne /app /data
USER bellenne
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
