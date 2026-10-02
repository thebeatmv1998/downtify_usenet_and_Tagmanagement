# cookies-keeper: a Chromium with a persistent profile whose YouTube
# session is automatically exported as a cookies.txt for Downtify.
#
# One-time setup: open the container's web UI (port 3000) and log into
# youtube.com. The dump loop then refreshes OUT_FILE (a cookies.txt)
# every DUMP_INTERVAL_SECONDS, and Downtify — pointed at the same file
# via DOWNTIFY_COOKIES_FILE — always has a live session.
FROM lscr.io/linuxserver/chromium:latest

RUN apk add --no-cache python3 py3-pip && \
    pip3 install --no-cache-dir --break-system-packages websocket-client

COPY dump_cookies.py /app/dump_cookies.py
COPY dump_loop.sh /app/dump_loop.sh
RUN chmod +x /app/dump_loop.sh /app/dump_cookies.py

# linuxserver/chromium appends CHROME_CLI to its Chromium launch: the
# DevTools port is how the dump loop reads the cookies (localhost only).
ENV CHROME_CLI="--remote-debugging-port=9222" \
    CDP_URL="http://127.0.0.1:9222" \
    OUT_FILE="/out/cookies.txt" \
    DUMP_INTERVAL_SECONDS="600"

COPY s6-rc.d /etc/s6-overlay/s6-rc.d/

VOLUME /out
