FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libgl1 libegl1 libgles2 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch keeps the image free of CUDA libraries.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
      torch==2.15.0 torchvision==0.29.2
COPY requirements.txt .
# ultralytics and mediapipe each pull a GUI OpenCV build; keep only the headless one.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y opencv-python opencv-contrib-python \
 && pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless==5.0.0.93

# Bake model weights into the image so the service never downloads at runtime.
RUN mkdir -p /models \
 && python -c "from ultralytics import YOLO; YOLO('/models/yolov8n.pt')" \
 && python -c "import urllib.request as u; b='https://storage.googleapis.com/mediapipe-models/pose_landmarker/'; [u.urlretrieve(f'{b}pose_landmarker_{m}/float16/1/pose_landmarker_{m}.task', f'/models/pose_landmarker_{m}.task') for m in ('lite', 'full')]"

ENV YOLO_AUTOINSTALL=false YOLO_OFFLINE=true YOLO_VERBOSE=false
COPY frame_reader.py pose_service.py caregiver.py ./
COPY viewer ./viewer
CMD ["python", "-u", "pose_service.py"]
