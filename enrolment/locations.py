"""Location codes a stored answer may hold for a location.

A module that replaces location codes (e.g. msr_etl, when a temporary code gets its real one) registers how to
find a code's former codes, so conditions on an answered code also match answers given with an older one. Without a
registered history a code matches only itself.
"""

_CODE_HISTORIES = []


def register_location_code_history(former_codes):
    """Add ``former_codes(location_type, code)``: the codes the location of ``code`` had before (any iterable)."""
    if former_codes not in _CODE_HISTORIES:
        _CODE_HISTORIES.append(former_codes)


def location_codes(location_type, code):
    """``code`` and every code it replaced, sorted."""
    codes = {code}
    for former_codes in _CODE_HISTORIES:
        codes.update(former_codes(location_type, code) or ())
    return sorted(codes)
