# Atlas Vision Node - multi-arch (linux/arm64 for Oracle A1, linux/amd64 for anything else), CPU only.
#   docker build -t atlas-node -f node/Dockerfile .
#   docker run -d --name atlas-node --restart unless-stopped \
#       -v /opt/atlas-node/config:/config -v /opt/atlas-node/models:/models \
#       -p 127.0.0.1:8765:8765 atlas-node
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DEBIAN_FRONTEND=noninteractive \
    ATLAS_NODE_CONFIG=/config/node.json ATLAS_MODELS=/models YOLO_CONFIG_DIR=/models/.ultralytics ATLAS_HEALTH_BIND=0.0.0.0 \
    OMP_NUM_THREADS=4

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /node
COPY node/requirements.txt /node/requirements.txt
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r /node/requirements.txt

COPY node/atlas_node.py node/node.example.json /node/
RUN mkdir -p /config /models

VOLUME ["/config", "/models"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/',timeout=4).status==200 else 1)"
CMD ["python", "/node/atlas_node.py"]
