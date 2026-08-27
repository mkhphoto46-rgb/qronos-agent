from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


class SourceKind(Enum):
    """What kind of site a result came from."""

    OFFICIAL = "official"
    REFERENCE = "reference"
    COMMUNITY = "community"
    WEB = "web"
    UNKNOWN = "unknown"


PERSIAN_LABEL: dict[SourceKind, str] = {
    SourceKind.OFFICIAL: "رسمی",
    SourceKind.REFERENCE: "مرجع",
    SourceKind.COMMUNITY: "انجمن",
    SourceKind.WEB: "وب",
    SourceKind.UNKNOWN: "نامشخص",
}


# Suffixes reserved for governments, universities and academic bodies.
OFFICIAL_SUFFIXES = (
    ".gov",
    ".gov.ir",
    ".edu",
    ".ac.ir",
    ".ac.uk",
    ".mil",
    ".int",
)

# Documentation and encyclopedic hosts.
REFERENCE_HOSTS = (
    "wikipedia.org",
    "wikimedia.org",
    "wiktionary.org",
    "wikidata.org",
    "developer.mozilla.org",
    "docs.python.org",
    "peps.python.org",
    "learn.microsoft.com",
    "docs.microsoft.com",
    "developer.apple.com",
    "developer.android.com",
    "rfc-editor.org",
    "ietf.org",
    "arxiv.org",
    "pubmed.ncbi.nlm.nih.gov",
    "doi.org",
)

# Any host whose first label is "docs", "developer" or "learn" is treated as
# documentation, which catches the long tail without listing every vendor.
REFERENCE_SUBDOMAIN_PREFIXES = ("docs.", "developer.", "learn.", "api.")

COMMUNITY_HOSTS = (
    "reddit.com",
    "stackoverflow.com",
    "stackexchange.com",
    "superuser.com",
    "serverfault.com",
    "askubuntu.com",
    "quora.com",
    "news.ycombinator.com",
    "discuss.python.org",
    "github.com",
    "gitlab.com",
)

COMMUNITY_PATTERNS = (
    r"\bforum",
    r"\bforums\b",
    r"\banjoman\b",
    r"\bdiscuss",
    r"\bcommunity\b",
)


@dataclass(frozen=True)
class Provenance:
    """Where a result came from, and how to label it."""

    url: str
    host: str
    kind: SourceKind

    @property
    def label(self) -> str:
        """Persian label for display next to the answer."""
        return PERSIAN_LABEL[self.kind]

    @property
    def is_recognised(self) -> bool:
        return self.kind is not SourceKind.UNKNOWN


def registrable_host(url: str) -> str:
    """
    Extract the host from a URL, lowercased, without a leading ``www.``.

    Returns an empty string for anything unparseable rather than raising: a
    malformed URL should produce an unlabelled result, not break the answer.
    """
    try:
        parsed = urlparse(url if "//" in url else f"//{url}", scheme="https")
    except ValueError:
        return ""

    host = (parsed.hostname or "").lower().strip()

    # urlparse will happily treat arbitrary text as a host, so "not a url at
    # all" would come back as a hostname. A real host has no whitespace and at
    # least one dot; anything else is reported as unparseable rather than
    # labelled as a website.
    if not host or " " in host or "." not in host:
        return ""

    if host.startswith("www."):
        host = host[4:]

    return host


def classify_source(
    url: str,
    subject_domains: tuple[str, ...] = (),
) -> Provenance:
    """
    Label a result by its domain.

    Entirely a lookup — no model involved, nothing measured, no judgement of
    whether a site is good. It reports what the domain is so the answer can
    show it.

    ``subject_domains`` lets a caller mark the official domain of whatever the
    query is about, so a question about Python can label ``python.org`` as
    official without that being hardcoded here.

    Checked in order of specificity: an explicitly supplied subject domain
    beats a suffix rule, which beats a host list, which beats a pattern.
    """
    host = registrable_host(url)

    if not host:
        return Provenance(url=url, host="", kind=SourceKind.UNKNOWN)

    for domain in subject_domains:
        candidate = domain.lower().lstrip(".")

        if candidate and (host == candidate or host.endswith(f".{candidate}")):
            return Provenance(url=url, host=host, kind=SourceKind.OFFICIAL)

    if any(host.endswith(suffix) for suffix in OFFICIAL_SUFFIXES):
        return Provenance(url=url, host=host, kind=SourceKind.OFFICIAL)

    for known in REFERENCE_HOSTS:
        if host == known or host.endswith(f".{known}"):
            return Provenance(url=url, host=host, kind=SourceKind.REFERENCE)

    if host.startswith(REFERENCE_SUBDOMAIN_PREFIXES):
        return Provenance(url=url, host=host, kind=SourceKind.REFERENCE)

    for known in COMMUNITY_HOSTS:
        if host == known or host.endswith(f".{known}"):
            return Provenance(url=url, host=host, kind=SourceKind.COMMUNITY)

    if any(re.search(pattern, host) for pattern in COMMUNITY_PATTERNS):
        return Provenance(url=url, host=host, kind=SourceKind.COMMUNITY)

    return Provenance(url=url, host=host, kind=SourceKind.WEB)


@dataclass(frozen=True)
class ProvenanceStrip:
    """The list of sources shown alongside an answer."""

    entries: tuple[Provenance, ...]

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def hosts(self) -> tuple[str, ...]:
        return tuple(entry.host for entry in self.entries)

    def render_persian(self) -> str:
        """
        Render the strip for display.

        Ordered as given, not re-sorted by kind: reordering would imply a
        ranking, and the strip does not rank.
        """
        if self.is_empty:
            return ""

        lines = [f"خوانده شد از {len(self.entries)} منبع:"]

        for entry in self.entries:
            lines.append(f"  {entry.host}  —  {entry.label}")

        return "\n".join(lines)


def build_strip(
    urls: tuple[str, ...],
    subject_domains: tuple[str, ...] = (),
) -> ProvenanceStrip:
    """
    Build a provenance strip from the URLs an answer was drawn from.

    Deduplicates by host, keeping first appearance, so three pages from one
    site show as one source rather than padding the list.
    """
    seen: set[str] = set()
    entries: list[Provenance] = []

    for url in urls:
        provenance = classify_source(url, subject_domains)

        if not provenance.host or provenance.host in seen:
            continue

        seen.add(provenance.host)
        entries.append(provenance)

    return ProvenanceStrip(entries=tuple(entries))


def main() -> None:
    """Show the lookup on a spread of hosts."""
    samples = (
        "https://fa.wikipedia.org/wiki/پایتون",
        "https://docs.python.org/3/library/dataclasses.html",
        "https://stackoverflow.com/questions/47955263/",
        "https://sharif.ac.ir/announcement",
        "https://blog.rayanekomak.com/show-all-file-and-sort-by-date/",
        "https://digiato.com/article/2019/06/17/foo",
        "https://forum.example.ir/thread/1",
        "not a url at all",
    )

    print("=== provenance lookup ===")

    strip = build_strip(samples)

    for entry in strip.entries:
        print(f"{entry.kind.value:11s} {entry.label:8s} {entry.host}")

    print()
    print(strip.render_persian())


if __name__ == "__main__":
    main()
