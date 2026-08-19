FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Explicit copy list -- never `COPY . .` -- so nothing outside these paths
# (in particular .env, which is never present in the build context anyway
# per .dockerignore, but this is belt-and-braces) can end up in the image.
COPY app/ app/
COPY data/copy_trader.db data/copy_trader.db

CMD ["python", "-m", "app.main"]
