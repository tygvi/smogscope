# SmogScope image: one image, used by two containers (scheduler and web).
# Built in two stages so the final image contains only what is needed to RUN the app.

# ---------------------------------------------------------------------------
# Stage 1 "web-build": compile the React/TypeScript site into plain HTML/JS/CSS.
# Node is needed only for this step; none of it ends up in the final image.
# ---------------------------------------------------------------------------
FROM node:22-slim AS web-build
WORKDIR /web

# Copy only the dependency list first: this layer (npm ci) is cached and reused
# until package.json / package-lock.json change, so editing a .tsx file stays fast.
COPY web/package.json web/package-lock.json ./
RUN npm ci

# Now the source code, then build -> /web/dist
COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 "app": the Python application that actually runs.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS app

# PYTHONUNBUFFERED: print logs immediately (otherwise `docker compose logs` lags behind).
# PYTHONDONTWRITEBYTECODE: no .pyc files inside the container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Same caching trick: install libraries before copying our code.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Our code, the SQL migrations, and the built website from stage 1.
COPY aqi_india/ aqi_india/
COPY sql/ sql/
COPY --from=web-build /web/dist web/dist

# Never run as root: if the app were ever compromised, the attacker would be a
# powerless user inside a throwaway container.
RUN useradd --create-home --uid 10001 smog
USER smog

EXPOSE 8000

# Default command = the web server. docker-compose.yml overrides it for the scheduler.
# --host 0.0.0.0: listen on all interfaces, otherwise nothing outside the container can reach it.
CMD ["uvicorn", "aqi_india.api:app", "--host", "0.0.0.0", "--port", "8000"]
