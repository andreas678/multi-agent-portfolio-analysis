FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/*.py /app/
COPY app/*.md /app/

ENV PYTHONUNBUFFERED=1 \
    PORTFOLIO_DATA=/data \
    PORTFOLIO_REPORTS=/reports

RUN useradd -m agent && \
    mkdir -p /data /reports && \
    chown -R agent /app /reports

USER agent

ENTRYPOINT ["python"]
CMD ["/app/supervisor.py", "/data/portfolio.xlsx"]
