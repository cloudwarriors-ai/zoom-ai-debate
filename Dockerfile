FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV PYTHONUNBUFFERED=1

# System dependencies for Zoom SDK (matches bighead)
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv python-is-python3 \
    xvfb \
    # Build tools for zoom-meeting-sdk
    build-essential cmake ninja-build \
    # OpenGL/X11 libraries required by Zoom SDK
    libgl1-mesa-glx libgl1-mesa-dri libglib2.0-0 \
    libxkbcommon0 libxkbcommon-x11-0 \
    libdbus-1-3 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
    libxcb-shape0 libxcb-xfixes0 libxcb-xinerama0 \
    libxcb-cursor0 libegl1 libxcomposite1 libxdamage1 \
    libxrandr2 libxtst6 libnss3 libnspr4 \
    libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libgbm1 libgtk-3-0 libpango-1.0-0 libcairo2 \
    fonts-liberation curl \
    && rm -rf /var/lib/apt/lists/*

# PyQt5 for Zoom SDK event loop
RUN apt-get update && apt-get install -y python3-pyqt5 && rm -rf /var/lib/apt/lists/*

# PulseAudio and ALSA
RUN apt-get update && apt-get install -y \
    pulseaudio pulseaudio-utils alsa-utils libasound2-plugins \
    && rm -rf /var/lib/apt/lists/*

# Route ALSA through PulseAudio
RUN printf 'pcm.!default {\n    type pulse\n}\nctl.!default {\n    type pulse\n}\n' > /etc/asound.conf

WORKDIR /app

# Python dependencies
RUN pip3 install --no-cache-dir \
    fastapi uvicorn websockets numpy pydantic python-dotenv pyjwt anthropic

# Zoom Meeting SDK (needs cmake + ninja installed above)
RUN pip3 install --no-cache-dir zoom-meeting-sdk

COPY backend/ ./backend/

ENV QT_QPA_PLATFORM=offscreen
ENV DISPLAY=:99

RUN printf '#!/bin/bash\n\
rm -f /tmp/.X99-lock && rm -rf /tmp/.X11-unix/X99 && rm -rf /run/user/0/pulse\n\
Xvfb :99 -screen 0 1280x1024x24 &\n\
sleep 2\n\
pulseaudio --start --exit-idle-time=-1 2>/dev/null || true\n\
sleep 2\n\
timeout 10 pactl load-module module-null-sink sink_name=virtual_speaker sink_properties=device.description=VirtualSpeaker || echo "WARN: pactl load-module timed out"\n\
timeout 5 pactl set-default-sink virtual_speaker || echo "WARN: pactl set-default-sink timed out"\n\
echo "Audio setup done"\n\
cd /app/backend && exec uvicorn main:app --host 0.0.0.0 --port 8000\n' > /app/start.sh && chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
