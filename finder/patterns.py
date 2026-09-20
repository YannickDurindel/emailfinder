"""Generate candidate email addresses from a person's name and a domain."""
from __future__ import annotations

import re
from dataclasses import dataclass


def _clean(part: str) -> str:
    """Lowercase and strip to plain ascii letters (drop accents, spaces, hyphens...)."""
    part = part.strip().lower()
    # keep it simple: strip anything that isn't a-z. Good enough for generating
    # guesses -- real mailboxes are almost always plain ascii.
    part = re.sub(r"[^a-z]", "", part)
    return part


@dataclass(frozen=True)
class Candidate:
    email: str
    pattern: str  # human-readable name of the pattern used, e.g. "first.last"
    tier: str = "personal"  # "personal" | "role" | "alt-domain"


# Common generic/role mailboxes worth trying at a company domain. These aren't
# tied to a specific person, so they're a lower-confidence guess than a name
# pattern -- useful as a fallback, not a first choice.
ROLE_LOCAL_PARTS: tuple[str, ...] = ("contact", "info", "sales", "support", "hello", "admin", "office")

# Reduced pattern set used when trying alternate TLDs of the same company
# name -- trying all 16 name patterns against every alternate domain would
# explode the candidate count for little added value.
_ALT_DOMAIN_PATTERNS = ("first.last", "flast")

DEFAULT_ALT_TLDS: tuple[str, ...] = ("fr", "co", "us")

# TLDs tried when discovering a company's real domain from just its name.
DEFAULT_DISCOVERY_TLDS: tuple[str, ...] = ("com", "net", "org", "io", "co", "fr", "de", "us", "biz", "info")

_COMPANY_SUFFIXES = ("inc", "llc", "ltd", "corp", "co", "sa", "gmbh", "srl", "bv")


def _slugify_company(company: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "", company.strip().lower())
    for suf in _COMPANY_SUFFIXES:
        if slug.endswith(suf) and len(slug) > len(suf):
            slug = slug[: -len(suf)]
            break
    return slug


def generate_candidates(first_name: str, last_name: str, domain: str) -> list[Candidate]:
    """Return likely email candidates, ordered from most to least common pattern.

    Order roughly follows observed frequency in corporate email conventions.
    """
    first = _clean(first_name)
    last = _clean(last_name)
    domain = domain.strip().lower().lstrip("@")

    if not first or not last:
        raise ValueError("first_name and last_name must contain at least one letter")
    if not domain or "." not in domain:
        raise ValueError(f"domain looks invalid: {domain!r}")

    f, l = first[0], last[0]

    patterns: list[tuple[str, str]] = [
        ("first.last", f"{first}.{last}"),
        ("firstlast", f"{first}{last}"),
        ("f.last", f"{f}.{last}"),
        ("flast", f"{f}{last}"),
        ("first", first),
        ("last", last),
        ("first_last", f"{first}_{last}"),
        ("first-last", f"{first}-{last}"),
        ("last.first", f"{last}.{first}"),
        ("lastfirst", f"{last}{first}"),
        ("last_first", f"{last}_{first}"),
        ("l.first", f"{l}.{first}"),
        ("lfirst", f"{l}{first}"),
        ("firstl", f"{first}{l}"),
        ("first.l", f"{first}.{l}"),
        ("last-first", f"{last}-{first}"),
    ]

    seen: set[str] = set()
    out: list[Candidate] = []
    for name, local_part in patterns:
        email = f"{local_part}@{domain}"
        if email in seen:
            continue
        seen.add(email)
        out.append(Candidate(email=email, pattern=name))
    return out


def generate_role_candidates(domain: str, roles: tuple[str, ...] = ROLE_LOCAL_PARTS) -> list[Candidate]:
    """Generic/role mailboxes at a domain, e.g. contact@, sales@, info@."""
    domain = domain.strip().lower().lstrip("@")
    seen: set[str] = set()
    out: list[Candidate] = []
    for role in roles:
        email = f"{role}@{domain}"
        if email in seen:
            continue
        seen.add(email)
        out.append(Candidate(email=email, pattern=f"role:{role}", tier="role"))
    return out


def alt_tld_domains(domain: str, tlds: tuple[str, ...] = DEFAULT_ALT_TLDS) -> list[str]:
    """Same company name, other common TLDs -- e.g. acme.com -> acme.fr, acme.co, acme.us."""
    domain = domain.strip().lower().lstrip("@")
    if "." not in domain:
        return []
    base, _, current_tld = domain.rpartition(".")
    if not base:
        return []
    seen = {current_tld}
    out: list[str] = []
    for tld in tlds:
        tld = tld.strip().lstrip(".").lower()
        if not tld or tld in seen:
            continue
        seen.add(tld)
        out.append(f"{base}.{tld}")
    return out


def generate_alt_domain_candidates(
    first_name: str,
    last_name: str,
    domain: str,
    tlds: tuple[str, ...] = DEFAULT_ALT_TLDS,
    include_roles: bool = True,
    roles: tuple[str, ...] = ROLE_LOCAL_PARTS,
) -> list[Candidate]:
    """Long-shot candidates at alternate-TLD versions of the same domain."""
    out: list[Candidate] = []
    for alt_domain in alt_tld_domains(domain, tlds=tlds):
        for c in generate_candidates(first_name, last_name, alt_domain):
            if c.pattern in _ALT_DOMAIN_PATTERNS:
                out.append(Candidate(email=c.email, pattern=c.pattern, tier="alt-domain"))
        if include_roles:
            for c in generate_role_candidates(alt_domain, roles=roles):
                out.append(Candidate(email=c.email, pattern=c.pattern, tier="alt-domain"))
    return out


def generate_full_candidates(
    first_name: str,
    last_name: str,
    domain: str,
    include_roles: bool = True,
    include_alt_domains: bool = True,
    alt_tlds: tuple[str, ...] = DEFAULT_ALT_TLDS,
) -> list[Candidate]:
    """The full, tiered candidate list: name-pattern guesses first (most
    likely), then generic role addresses, then long-shot alternate-TLD
    guesses -- in that order, so verification checks the likeliest hits
    first.
    """
    out = list(generate_candidates(first_name, last_name, domain))
    if include_roles:
        out.extend(generate_role_candidates(domain))
    if include_alt_domains:
        out.extend(generate_alt_domain_candidates(first_name, last_name, domain, tlds=alt_tlds, include_roles=include_roles))

    seen: set[str] = set()
    deduped: list[Candidate] = []
    for c in out:
        if c.email in seen:
            continue
        seen.add(c.email)
        deduped.append(c)
    return deduped


def generate_candidates_for_domains(
    first_name: str,
    last_name: str,
    domains: list[str],
    include_roles: bool = True,
) -> list[Candidate]:
    """Full name-pattern + role candidates for each of several *confirmed*
    domains (see verify.discover_domains) -- no reduced pattern set and no
    further alt-TLD guessing, since these domains are already known to
    exist rather than being unconfirmed long shots.
    """
    out: list[Candidate] = []
    for domain in domains:
        out.extend(generate_candidates(first_name, last_name, domain))
        if include_roles:
            out.extend(generate_role_candidates(domain))

    seen: set[str] = set()
    deduped: list[Candidate] = []
    for c in out:
        if c.email in seen:
            continue
        seen.add(c.email)
        deduped.append(c)
    return deduped


def guess_domain_from_company(company: str, tld: str = "com") -> str:
    """Best-effort slug of a company name into a single domain guess. Not
    reliable on its own -- prefer generate_domain_guesses() + a DNS check
    (see verify.discover_domains) to confirm a domain actually exists
    before relying on it.
    """
    return f"{_slugify_company(company)}.{tld}"


def generate_domain_guesses(company: str, tlds: tuple[str, ...] = DEFAULT_DISCOVERY_TLDS) -> list[str]:
    """All candidate domains for a company name across several common TLDs,
    e.g. "Acme Inc" -> ["acme.com", "acme.net", "acme.org", ...]. These are
    unconfirmed guesses -- see verify.discover_domains to check which ones
    actually exist before using them.
    """
    slug = _slugify_company(company)
    if not slug:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tld in tlds:
        tld = tld.strip().lstrip(".").lower()
        if not tld:
            continue
        domain = f"{slug}.{tld}"
        if domain in seen:
            continue
        seen.add(domain)
        out.append(domain)
    return out
