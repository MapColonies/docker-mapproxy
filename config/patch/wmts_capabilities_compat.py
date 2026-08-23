#!/usr/bin/env python3
"""Fix the invalid <ows:SupportedCRS> urn MapProxy emits since 6.0.0.

MapProxy commit b8f8949b ("restful encoding / style isdefault and
urn:ogc:def:crs for SupportedCRS", first released in 6.0.0) gave
<ows:SupportedCRS> in the WMTS 1.0.0 capabilities template a
"urn:ogc:def:crs:" prefix rendered as "urn:ogc:def:crs:EPSG:4326" — an
invalid OGC URN: the version field between authority and code is missing,
so there is a single colon where the standard (OGC 07-092r1) requires two
("urn:ogc:def:crs:EPSG::4326").  This script keeps the prefix but doubles
the first colon of srs_name at render time, producing the valid
"urn:ogc:def:crs:EPSG::4326" form.

This script edits exactly that one line in the installed template and
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
        'SupportedCRS urn missing version colon',
        '<ows:SupportedCRS>urn:ogc:def:crs:{{tile_matrix_set.srs_name}}</ows:SupportedCRS>',
        "<ows:SupportedCRS>urn:ogc:def:crs:"
        "{{tile_matrix_set.srs_name.replace(':', '::', 1)}}"
        '</ows:SupportedCRS>',
        1,
    ),
)

# Text that must not survive the rewrite, whatever form the template took.
RESIDUE = ('urn:ogc:def:crs:{{tile_matrix_set.srs_name}}',)


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

    print('[patch] wmts100capabilities.xml: fixed SupportedCRS urn to the '
          'valid urn:ogc:def:crs:EPSG::<code> form')


if __name__ == '__main__':
    main(sys.argv)
