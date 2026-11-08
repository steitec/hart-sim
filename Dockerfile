# hart-sim in a container: by default a pressure transmitter (PT-101, polling
# address 1) and a temperature transmitter (TT-201, polling address 2) behind one
# HART-IP port. Mount your own bench file over /bench/bench.yaml.
#
# The control channel listens on all interfaces inside the container, because
# nothing could reach it otherwise. It has no authentication, so publish it on
# the host's loopback only.
#
#   docker run --rm -p 5094:5094 -p 5094:5094/udp -p 127.0.0.1:5095:5095 ghcr.io/steitec/hart-sim
#   docker run --rm -p 5094:5094 -v ./bench.yaml:/bench/bench.yaml ghcr.io/steitec/hart-sim
FROM python:3.13-slim
LABEL org.opencontainers.image.source=https://github.com/steitec/hart-sim \
      org.opencontainers.image.description="Simulated HART field devices speaking HART-IP, with fault injection" \
      org.opencontainers.image.licenses=MIT

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /src

COPY examples/plant.yaml /bench/bench.yaml
RUN useradd --create-home --uid 10001 hart
USER hart
WORKDIR /bench

EXPOSE 5094/tcp 5094/udp 5095/tcp
# Healthy once the HART-IP port accepts connections. A service container in GitHub
# Actions waits for this.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 5094), 2).close()"]
ENTRYPOINT ["hart-sim"]
CMD ["serve", "--bench", "/bench/bench.yaml", "--host", "0.0.0.0", "--control-host", "0.0.0.0"]
