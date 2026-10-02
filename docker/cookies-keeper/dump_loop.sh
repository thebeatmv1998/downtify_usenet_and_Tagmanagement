#!/usr/bin/env sh
# s6 longrun: refresh the cookies.txt forever.
exec python3 /app/dump_cookies.py
