FROM python:3.12-slim
# pip reads PIP_INDEX_URL from the environment; override at build time for a closer mirror.
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY inventory inventory
COPY profiles profiles
ENV DATA_DIR=/data
EXPOSE 8000
CMD ["uvicorn", "inventory.app:app", "--host", "0.0.0.0", "--port", "8000"]
