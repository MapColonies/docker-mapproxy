#!/usr/bin/env python3
"""Restore pre-6.0 WMTS GetCapabilities output for two upstream changes.

MapProxy commit b8f8949b ("restful encoding / style isdefault and
urn:ogc:def:crs for SupportedCRS", first released in 6.0.0) changed the WMTS
1.0.0 capabilities template in two ways that break our existing clients:

  1. <Style> gained an isDefault="true" attribute.
  2. <ows:SupportedCRS> gained a "urn:ogc:def:crs:" prefix, so a grid on
     EPSG:4326 now advertises "urn:ogc:def:crs:EPSG:4326".

This script reverts exactly those two edits in the installed template and
leaves every other upstream change in place.  It is applied at build time
(see the Dockerfile) rather than shipping a full copy of the template, so a
MapProxy upgrade that reworks these lines aborts the build loudly instead of
silently reverting unrelated template improvements.

Usage: wmts_capabilities_compat.py <path-to-wmts100capabilities.xml>
"""

import sys

# (description, exact text in the installed template, replacement, expected hits)
EDITS = (
    (
        'Style isDefault attribute',
        '<Style isDefault="true">',
        '<Style>',
        1,
    ),
    (
        'SupportedCRS urn prefix',
        '<ows:SupportedCRS>urn:ogc:def:crs:{{tile_matrix_set.srs_name}}</ows:SupportedCRS>',
        '<ows:SupportedCRS>{{tile_matrix_set.srs_name}}</ows:SupportedCRS>',
        1,
    ),
)

# Text that must not survive the rewrite, whatever form the template took.
RESIDUE = ('isDefault', 'urn:ogc:def:crs:')


def main(argv):
    if len(argv) != 2:
        sys.exit('usage: %s <path-to-wmts100capabilities.xml>' % argv[0])

    path = argv[1]
    with open(path, encoding='utf-8') as fh:
        template = fh.read()

    for description, old, new, expected in EDITS:
        found = template.count(old)
        if found != expected:
            sys.exit(
                '[patch] %s: expected %d occurrence(s) of %r in %s, found %d — '
                'upstream template changed, review the patch against this '
                'MapProxy version' % (description, expected, old, path, found)
            )
        template = template.replace(old, new)

    for residue in RESIDUE:
        if residue in template:
            sys.exit(
                '[patch] %r still present in %s after rewrite' % (residue, path)
            )

    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(template)

    print('[patch] wmts100capabilities.xml: removed Style/isDefault and '
          'SupportedCRS urn:ogc:def:crs: prefix')


if __name__ == '__main__':
    main(sys.argv)
