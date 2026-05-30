# =============================================================================
#  PumpSleeper — Multi-arch Docker image
#  Runs the proxy server + dashboard on any Linux machine.
#
#  Build:
#    docker build -t pumpsleeper .
#
#  For multi-arch (ARM64 + AMD64):
#    docker buildx build --platform linux/amd64,linux/arm64 -t pumpsleeper .
# =============================================================================

FROM python:3.11-slim

LABEL org.opencontainers.image.title="PumpSleeper" \
      org.opencontainers.image.description="Local proxy and dashboard for PumpSpy sump pump monitors" \
      org.opencontainers.image.source="https://github.com/pgoutsos/pumpsleeper"

# Install Python dependencies
COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy app files
WORKDIR /app
COPY app/server.py    .
COPY app/dashboard.py .
COPY app/db.py        .
COPY app/mqtt.py      .

# Data directory (mount a volume here for persistence)
RUN mkdir -p /data
ENV PUMPSPY_DATA=/data

# Expose ports
EXPOSE 8081
EXPOSE 8080

# Default command runs both server and dashboard via a simple supervisor script
COPY docker-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
