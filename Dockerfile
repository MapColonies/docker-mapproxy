#--------- Generic stuff all our Dockerfiles should start with so we get caching ------------
FROM python:3.10.6
MAINTAINER Tim Sutton<tim@kartoza.com>

#-------------Application Specific Stuff ----------------------------------------------------
RUN apt-get -y update && \
    apt-get install -y \
    python3-virtualenv \
    libgeos-dev \
    python3-lxml \
    libgdal-dev \
    #built pillow 
    #python-pil \
    #depecndencies for using new pillow source
    build-essential \
    python-dev \
    libjpeg-dev \
    zlib1g-dev \
    libfreetype6-dev 
#pip is used instead of the following
#python-yaml \
#python-shaply \
#libproj12 \


#    gettext \
#gosu awscli; \
# verify that the binary works
#gosu nobody true

COPY requirements.txt /requirements.txt
RUN pip install -r requirements.txt

# Cleanup resources
RUN apt-get -y --purge autoremove  \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8080
ENV \
    # Run
    PROCESSES=6 \
    THREADS=10 \
    # Run using uwsgi. This is the default behaviour. Alternatively run using the dev server. Not for production settings
    PRODUCTION=true \
    TELEMETRY_TRACING_ENABLED='true' \
    # Set telemetry endpoint
    TELEMETRY_ENDPOINT='localhost:4317' \
    OTEL_RESOURCE_ATTRIBUTES='service.name=mapcolonies,application=mapproxy' \
    OTEL_SERVICE_NAME='mapproxy' \
    TELEMETRY_SAMPLING_RATIO_DENOMINATOR=1000

ADD uwsgi.ini /settings/uwsgi.default.ini
ADD start.sh /start.sh
RUN chmod 0755 /start.sh
RUN mkdir -p /mapproxy /settings
ADD log.ini /mapproxy/log.ini
ADD authFilter.py /mapproxy/authFilter.py
ADD app.py /mapproxy/app.py

RUN chgrp -R 0 /mapproxy /settings /start.sh && \
    chmod -R g=u /mapproxy /settings /start.sh
RUN useradd -ms /bin/bash user && usermod -a -G root user
USER user
VOLUME [ "/mapproxy"]
# USER mapproxy
ENTRYPOINT [ "/start.sh" ]
CMD ["mapproxy-util", "serve-develop", "-b", "0.0.0.0:8080", "mapproxy.yaml"]
