"""On-demand DNS propagation check: query a spread of independent public
resolvers directly and compare each answer to this server's own IP.

This is not a simulation of "what every country's ISP sees" -- most of the
resolvers below are anycast, so a query from this one server lands on
whichever of their edges is nearest to it, not the resolver's nominal
country. What it *does* show honestly: whether a handful of independently
operated resolver networks have already picked up the current record, which
is exactly what you want to know right after pointing a domain here.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional

import dns.resolver

logger = logging.getLogger(__name__)

# (label, resolver IP, home network -- informational only, not a probe location)
PUBLIC_RESOLVERS = [
    ("Google Public DNS", "8.8.8.8", "Global anycast"),
    ("Cloudflare", "1.1.1.1", "Global anycast"),
    ("Quad9", "9.9.9.9", "Switzerland, anycast"),
    ("OpenDNS (Cisco)", "208.67.222.222", "Global anycast"),
    ("Verisign", "64.6.64.6", "United States"),
    ("DNS.WATCH", "84.200.69.80", "Germany"),
    ("Yandex DNS", "77.88.8.8", "Russia"),
    ("AliDNS", "223.5.5.5", "China, anycast"),
    ("AdGuard DNS", "94.140.14.14", "Cyprus, anycast"),
    ("CleanBrowsing", "185.228.168.9", "Global anycast"),
]

LOOKUP_TIMEOUT = 4.0


@dataclass
class ResolverResult:
    label: str
    resolver_ip: str
    region: str
    answers: List[str]
    matches: Optional[bool]
    error: Optional[str] = None


def _query_one(domain: str, label: str, resolver_ip: str, region: str) -> ResolverResult:
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [resolver_ip]
    resolver.timeout = LOOKUP_TIMEOUT
    resolver.lifetime = LOOKUP_TIMEOUT
    try:
        answer = resolver.resolve(domain, "A")
        ips = sorted({str(record) for record in answer})
        return ResolverResult(label, resolver_ip, region, ips, None)
    except dns.resolver.NXDOMAIN:
        return ResolverResult(label, resolver_ip, region, [], None, error="No such domain")
    except dns.resolver.NoAnswer:
        return ResolverResult(label, resolver_ip, region, [], None, error="No A record")
    except Exception as exc:  # noqa: BLE001 - timeouts, refused, unreachable resolver, etc.
        logger.debug("dns check: %s via %s failed: %s", domain, resolver_ip, exc)
        return ResolverResult(label, resolver_ip, region, [], None, error="Unreachable")


def check_propagation(domain: str, expected_ip: Optional[str]) -> List[ResolverResult]:
    """Query every resolver in parallel; each takes at most LOOKUP_TIMEOUT."""
    with ThreadPoolExecutor(max_workers=len(PUBLIC_RESOLVERS)) as pool:
        results = list(
            pool.map(
                lambda entry: _query_one(domain, *entry),
                PUBLIC_RESOLVERS,
            )
        )

    if expected_ip:
        for result in results:
            if result.answers:
                result.matches = expected_ip in result.answers

    return results
