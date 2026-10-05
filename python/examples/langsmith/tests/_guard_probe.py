"""A test-only probe: try three non-loopback connections under the loopback guard.

The contract test runs this through ``_guarded_run.py`` to prove the guard
really refuses and logs a non-loopback attempt, so the ``guard_attempts == []``
assertions elsewhere cannot pass because the guard silently stopped working.
The targets are reserved and never reachable: TEST-NET-1 (RFC 5737), the IPv6
documentation prefix (RFC 3849) and the ``.invalid`` TLD (RFC 2606).
Not part of the recipe.
"""

from __future__ import annotations

import socket

refused = []
for label, attempt in (
    ("connect", lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("192.0.2.1", 80))),
    ("connect_ex", lambda: socket.socket(socket.AF_INET6, socket.SOCK_STREAM).connect_ex(("2001:db8::1", 80, 0, 0))),
    ("getaddrinfo", lambda: socket.getaddrinfo("example.invalid", 443)),
):
    try:
        attempt()
    except OSError as error:
        if "blocked by loopback guard" in str(error):
            refused.append(label)
print("refused:", ",".join(refused))
