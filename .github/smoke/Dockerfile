FROM python:3.14-slim

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

CMD ["python", "-c", "import smartthings_local, cbor2"]
