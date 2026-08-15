FROM python:3.14

WORKDIR /app

COPY requirements.txt migration.py soulseekarr.py ./
COPY resources/ resources/

RUN apt-get update \
    && apt-get install -y tini \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1
ENV IN_DOCKER=Yes

ENTRYPOINT ["tini", "-g", "--", "python", "-u", "/app/soulseekarr.py"]
