# RedFlag: the agent API and the web app in one image.
#   docker build -t redflag .
#   docker run --rm -p 8000:8000 -e SAMPLE_MODE=true redflag            # no Zetaris/Nightshift needed
#   docker run --rm -p 8000:8000 --env-file .env redflag                # live; use host.docker.internal in .env

FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=web /web/dist ./web/dist
RUN mkdir -p logs output
EXPOSE 8000
CMD ["python", "run.py", "serve"]
