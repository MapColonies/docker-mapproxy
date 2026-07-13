# This file is part of the MapProxy project.
# Copyright (C) 2017 Omniscale <http://omniscale.de>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
import datetime

import hashlib
import os
import time
from io import BytesIO

from mapproxy.image import ImageSource
from mapproxy.cache.base import (
    TileCacheBase,
    tile_buffer,
)

try:
    import redis
    from redis.retry import Retry
    from redis.backoff import NoBackoff
except ImportError:
    redis = None


import logging
log = logging.getLogger(__name__)


class RedisCache(TileCacheBase):
    def __init__(
            self, host, port, prefix, ttl=0, db=0, username=None, password=None,
            coverage=None, ssl_certfile=None, ssl_keyfile=None, ssl_ca_certs=None):
        super().__init__(coverage=coverage)
        if redis is None:
            raise ImportError("Redis backend requires 'redis' package.")

        self.prefix = prefix
        self.lock_cache_id = 'redis-' + hashlib.md5((host + str(port) + prefix + str(db)).encode('utf-8')).hexdigest()
        self.ttl = ttl
        # Set a operation timeout nonnegative, floating point number expressing *seconds*.
        self.socket_timeout = float(os.environ.get('SOCKET_TIMEOUT_SECONDS', "0.1"))
        # Set a connection timeout, nonnegative floating point number expressing *seconds*.
        self.socket_connection_timeout = float(os.environ.get('SOCKET_CONNECTION_TIMEOUT_SECONDS', "0.1"))

        ssl_enabled = get_redis_variable("REDIS_TLS")
        cert_reqs = os.environ.get("SSL_CERT_REQS", None)
        health_check_interval = int(os.environ.get("REDIS_HEALTH_CHECK_INTERVAL", "0"))
        # redis-py >= 6 retries every command 3 times with exponential backoff
        # (1-10s sleeps) by default, so a down Redis stalls each cache call for
        # ~5s instead of failing within socket_timeout. Zero retries makes a
        # failed Redis fall through to the next cache immediately.
        retry_attempts = int(os.environ.get("REDIS_RETRY_ATTEMPTS", "0"))

        redis_kwargs = {
            "host": host,
            "port": port,
            "db": db,
            "password": password,
            "socket_timeout": self.socket_timeout,
            "socket_connect_timeout": self.socket_connection_timeout,
            "socket_keepalive": True,
            "health_check_interval": health_check_interval,
            "ssl": ssl_enabled,
            "ssl_cert_reqs": cert_reqs,
            "ssl_certfile": ssl_certfile,
            "ssl_keyfile": ssl_keyfile,
            "ssl_ca_certs": ssl_ca_certs,
            "retry_on_timeout": False,
            "retry": Retry(NoBackoff(), retry_attempts),
        }

        if username:
            redis_kwargs["username"] = username

        self.r = redis.StrictRedis(**redis_kwargs)


    def _key(self, tile):
        x, y, z = tile.coord
        return self.prefix + '-%d-%d-%d' % (z, x, y)

    def is_cached(self, tile, dimensions=None):
        if tile.coord is None or tile.source:
            return True
        key = self._key(tile)

        try:
            log.debug('exists_key, key: %s' % key)
            return self.r.exists(key)
        except redis.exceptions.TimeoutError as e:
            log.error('REDIS:exists_key timeout error, returning false. %s' % e)
            return False
        except redis.exceptions.ConnectionError as e:
            log.error('Error during connection %s' % e)
            return False

    def store_tile(self, tile, dimensions=None):
        if tile.stored:
            return True
        key = self._key(tile)

        with tile_buffer(tile) as buf:
            data = buf.read()

        try:
            log.debug('store_key, key: %s' % key)
            r = self.r.set(key, data)
            if self.ttl:
                # use ms expire times for unit-tests
                self.r.pexpire(key, int(self.ttl * 1000))
        except redis.exceptions.TimeoutError as e:
            log.error('REDIS:store_key timeout error, returning false. %s' % e)
            return False
        except redis.exceptions.ConnectionError as e:
            log.error('Error during connection %s' % e)
            return False

        return r

    def load_tile(self, tile, with_metadata=False, dimensions=None):
        if tile.source or tile.coord is None:
            return True
        key = self._key(tile)

        try:
            log.debug('get_key, key: %s' % key)
            tile_data = self.r.get(key)
            if tile_data:
                tile.source = ImageSource(BytesIO(tile_data))
                return True
            return False
        except redis.exceptions.TimeoutError as e:
            log.error('REDIS:get_key timeout error, returning false. %s' % e)
            return False
        except redis.exceptions.ConnectionError as e:
            log.error('Error during connection %s' % e)
            return False

    def load_tile_metadata(self, tile, dimensions=None):
        if tile.timestamp:
            return
        try:
            pipe = self.r.pipeline()
            pipe.ttl(self._key(tile))
            pipe.memory_usage(self._key(tile))
            pipe_res = pipe.execute()
            tile.timestamp = time.mktime(datetime.datetime.now().timetuple()) - self.ttl - int(pipe_res[0])
            tile.size = pipe_res[1]
        except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError) as e:
            log.error('REDIS:load_tile_metadata error %s' % e)
            # Fail silently so the worker doesn't crash.
            pass
        
    def remove_tile(self, tile, dimensions=None):
        if tile.coord is None:
            return True

        key = self._key(tile)
        try:
            self.r.delete(key)
            return True
        except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError) as e:
            log.error('REDIS:remove_tile error %s' % e)
            return False

def get_redis_variable(name):
    env_var = os.environ.get(name, "false")
    if env_var.lower().strip() == "true":
        return True
    else:   
        return False
