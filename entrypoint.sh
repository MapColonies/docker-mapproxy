#!/bin/sh
set -e

# Create directories MapProxy needs for locks, cache, and the uWSGI master FIFO.
# /uwsgi_config is provided by the shared-config emptyDir volume in Kubernetes;
# mkdir -p is a no-op when the volume is already mounted.
mkdir -p /tmp/mapproxy/locks /tmp/mapproxy/cache /uwsgi_config

# If no arguments were passed (or the default CMD "uwsgi" is given), launch
# uWSGI using the ini file.  In Kubernetes the helm-rendered ConfigMap is
# mounted at /mapproxy/uwsgi.ini and overrides the image default at runtime.
if [ "$1" = "uwsgi" ] || [ $# -eq 0 ]; then
    # /mapproxy/uwsgi.ini is provided by the helm-rendered ConfigMap mounted at
    # runtime.  Fail fast with a clear message rather than a cryptic uWSGI error.
    if [ ! -f /mapproxy/uwsgi.ini ]; then
        echo "[entrypoint] ERROR: /mapproxy/uwsgi.ini not found." >&2
        echo "[entrypoint] Mount the helm-rendered ConfigMap volume at /mapproxy/uwsgi.ini." >&2
        exit 1
    fi
    exec uwsgi --ini /mapproxy/uwsgi.ini
fi

# For any other command (e.g. "bash", "python", debugging), exec it directly.
exec "$@"
