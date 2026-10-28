# PN — image du service (le code est lu depuis le dépôt monté en /data/repo).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
ENV PYTHONUNBUFFERED=1 TZ=Europe/Paris
WORKDIR /data/repo
CMD ["python", "serveur/pn_bot.py"]
