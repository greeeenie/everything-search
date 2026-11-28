FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates chromium curl fonts-liberation x11-utils xauth xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 chrome \
    && useradd --uid 10001 --gid chrome --create-home chrome \
    && install -d -m 0700 -o chrome -g chrome /profile

COPY --chmod=0755 entrypoint.sh /usr/local/bin/chrome-entrypoint
ENV HOME=/home/chrome
USER chrome
ENTRYPOINT ["/usr/local/bin/chrome-entrypoint"]
