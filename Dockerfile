FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first for layer caching. Installed straight into system site-packages --
# no uv/venv needed in the image.
COPY pyproject.toml ./
RUN pip install --no-cache-dir "zeroconf>=0.132"

COPY relay/ ./relay/
COPY main.py config.toml ./

CMD ["python", "main.py"]
