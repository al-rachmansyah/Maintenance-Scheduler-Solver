FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY src/ src/
COPY data/ data/

# Cloud Run sets $PORT itself; app/server.py already reads it. HOST must be
# 0.0.0.0 for Cloud Run to reach the container. SOURCE=none makes every fresh
# deploy start unconfigured, so judges see the upload/reset onboarding flow
# rather than data baked into the image.
ENV HOST=0.0.0.0
ENV SOURCE=none

CMD ["python", "app/server.py"]
