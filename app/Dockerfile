FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --create-home --uid 10001 app
COPY *.py ./

# Run unprivileged. httpfs is installed as this user so it lands in their ~/.duckdb,
# baked into the image so tasks don't need to download it at runtime.
USER app
RUN python -c "import duckdb; duckdb.sql('INSTALL httpfs')"

EXPOSE 8501
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false"]
