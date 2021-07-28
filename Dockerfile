#--------- Generic stuff all our Dockerfiles should start with so we get caching ------------
FROM python:3.7
MAINTAINER Tim Sutton<tim@kartoza.com>
RUN apt-get -y update
#-------------Application Specific Stuff ----------------------------------------------------
RUN apt-get install -y \
    gettext \
    python-yaml \
    libgeos-dev \
    python-lxml \
    libgdal-dev \
    build-essential \
    python-dev \
    libjpeg-dev \
    zlib1g-dev \
    libfreetype6-dev \
    python-virtualenv
RUN pip install Shapely Pillow MapProxy uwsgi boto3 botocore \
    opentelemetry-api \
    opentelemetry-sdk \
    opentelemetry-exporter-otlp \
    opentelemetry-instrumentation-wsgi \
    opentelemetry-instrumentation-sqlite3 \
    opentelemetry-instrumentation-boto
EXPOSE 8080
ENV \
    # Run
    PROCESSES=6 \
    THREADS=10 \
    # Run using uwsgi. This is the default behaviour. Alternatively run using the dev server. Not for production settings
    PRODUCTION=true \
    # Set telemetry endpoint
    TELEMETRY_ENDPOINT='localhost:8080' \
    OTEL_SERVICE_NAME='mapproxy'

ADD uwsgi.ini /settings/uwsgi.default.ini
ADD app.py /app.py
ADD start.sh /start.sh
RUN chmod 0755 /start.sh
RUN mkdir -p /mapproxy /settings

# RUN groupadd -r mapproxy -g 10001 && \
# RUN groupadd -r mapproxy -g 10001 && \
#     useradd -m -d /home/mapproxy/ --gid 10001 -s /bin/bash -G mapproxy mapproxy
# RUN chown -R mapproxy:mapproxy /mapproxy /settings /start.sh
RUN chgrp -R 0 /mapproxy /settings /start.sh && \
    chmod -R g=u /mapproxy /settings /start.sh
RUN useradd -ms /bin/bash user && usermod -a -G root user
USER user
VOLUME [ "/mapproxy"]
# USER mapproxy
ENTRYPOINT [ "/start.sh" ]
CMD ["mapproxy-util", "serve-develop", "-b", "0.0.0.0:8080", "mapproxy.yaml"]
