# Changelog

## [2.1.2](https://github.com/MapColonies/docker-mapproxy/compare/v2.1.1...v2.1.2) (2026-08-19)


### Bug Fixes

* sign use_http_get reads so private S3/MinIO buckets work MAPCO-11391 ([#95](https://github.com/MapColonies/docker-mapproxy/issues/95)) ([5450af2](https://github.com/MapColonies/docker-mapproxy/commit/5450af2a20de07cfd2ffa4f73565b9e81426514b))

## [2.1.1](https://github.com/MapColonies/docker-mapproxy/compare/v2.1.0...v2.1.1) (2026-08-03)


### Bug Fixes

* upgrade nginx chart to 2.3.1 and drop pinned image tag ([#91](https://github.com/MapColonies/docker-mapproxy/issues/91)) ([b24c9fa](https://github.com/MapColonies/docker-mapproxy/commit/b24c9faff5bd0787032c1cc874eb77645fdb0574))

## [2.1.0](https://github.com/MapColonies/docker-mapproxy/compare/v2.0.0...v2.1.0) (2026-08-02)


### Miscellaneous Chores

* force alignment to rc track ([9a8e437](https://github.com/MapColonies/docker-mapproxy/commit/9a8e4374afd9d677dc7f2bce68126040c53b160d))

## [2.0.0](https://github.com/MapColonies/docker-mapproxy/compare/v1.9.2...v2.0.0) (2026-07-19)


### ⚠ BREAKING CHANGES

* docker-mapproxy is now the MapProxy 6 rewrite. MapProxy 1.13, the in-app authFilter, start.sh/uwsgi.ini/log.ini, and the requirements.txt dependency set are removed. mc-mapproxy is superseded.

### Features

* rewrite as MapProxy 6 image, v2.0.0 (supersedes mc-mapproxy) ([#86](https://github.com/MapColonies/docker-mapproxy/issues/86)) ([e9376a0](https://github.com/MapColonies/docker-mapproxy/commit/e9376a0296bc65cd8eb408eceb12d93a30559393))
