FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DASH_HOST=0.0.0.0 \
    DASH_PORT=5055

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard.py dashboard.html team.html build_qc.py export_qc.py target_counties.txt ./
COPY deploy/sql/ deploy/sql/

RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 5055
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5055/api/config').status==200 else 1)"

CMD ["python", "dashboard.py"]
