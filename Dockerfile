FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV QT_QPA_PLATFORM=offscreen

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv \
    pulseaudio \
    xvfb \
    redis-server \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY docs/ ./docs/

EXPOSE 8000

CMD ["python3", "backend/main.py"]
