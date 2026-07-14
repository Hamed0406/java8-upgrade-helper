FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY server.py scan.py index.html ./

EXPOSE 8765

CMD ["python3", "server.py"]
