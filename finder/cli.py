from __future__ import annotations

import argparse
import csv
import json
import sys

from .patterns import DEFAULT_ALT_TLDS, generate_full_candidates, guess_domain_from_company

STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_UNKNOWN = "unknown"
STATUS_CATCH_ALL = "catch_all_domain"
STATUS_ERROR = "error"

STATUS_ORDER = {STATUS_VALID: 0, STATUS_CATCH_ALL: 1, STATUS_UNKNOWN: 2, STATUS_ERROR: 3, STATUS_INVALID: 4}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="email-finder",
        description="Generate likely email addresses for a person at a company, and optionally "
        "test them with an SMTP handshake (no email is ever sent).",
    )
    p.add_argument("--first", required=True, help="First name")
    p.add_argument("--last", required=True, help="Last name")

    domain_group = p.add_mutually_exclusive_group(required=True)
    domain_group.add_argument("--domain", help="Company email domain, e.g. example.com")
    domain_group.add_argument(
        "--company",
        help="Company name; a domain will be GUESSED (best-effort, unreliable) as <slug>.com. "
        "Prefer --domain with the real domain whenever you know it.",
    )

    p.add_argument("--tld", default="com", help="TLD to use when guessing a domain from --company (default: com)")
    p.add_argument(
        "--no-role-addresses",
        action="store_true",
        help="Skip generic/role mailboxes (contact@, sales@, info@, support@, ...)",
    )
    p.add_argument(
        "--no-alt-tlds",
        action="store_true",
        help="Skip long-shot guesses at alternate-TLD versions of the domain (e.g. .fr/.co/.us)",
    )
    p.add_argument(
        "--alt-tlds",
        default=",".join(DEFAULT_ALT_TLDS),
        help=f"Comma-separated alternate TLDs to try (default: {','.join(DEFAULT_ALT_TLDS)})",
    )
    p.add_argument("--no-verify", action="store_true", help="Only generate candidates, skip SMTP verification")
    p.add_argument("--stop-on-valid", action="store_true", help="Stop verifying as soon as one candidate comes back valid")
    p.add_argument("--delay", type=float, default=1.5, help="Seconds to wait between SMTP checks (default: 1.5)")
    p.add_argument("--timeout", type=float, default=10.0, help="SMTP connection timeout in seconds (default: 10)")
    p.add_argument(
        "--helo-domain",
        default=None,
        help="Domain to identify as in EHLO/MAIL FROM (default: the target domain itself). "
        "Using a real, resolvable domain you control reduces the chance of being rejected as spam.",
    )
    p.add_argument("--mail-from", default=None, help="Full MAIL FROM address to use (default: verify@<helo-domain>)")
    p.add_argument(
        "--max-concurrent-domains",
        type=int,
        default=4,
        help="Check up to this many distinct domains at once (e.g. the alt-TLD long shots run in "
        "parallel with the main domain). Never opens more than one connection to the same server "
        "at a time. Default: 4. Set to 1 to fully serialize.",
    )
    p.add_argument("--output", choices=["table", "csv", "json"], default="table", help="Output format (default: table)")
    p.add_argument("--outfile", help="Write output to this file instead of stdout")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.domain:
        domain = args.domain.strip().lower()
    else:
        domain = guess_domain_from_company(args.company, tld=args.tld)
        print(f"[!] No --domain given; guessing '{domain}' from company name '{args.company}'.", file=sys.stderr)
        print("    This is a naive guess and is frequently wrong. Pass --domain if you know it.", file=sys.stderr)

    alt_tlds = tuple(t.strip() for t in args.alt_tlds.split(",") if t.strip())

    try:
        candidates = generate_full_candidates(
            args.first,
            args.last,
            domain,
            include_roles=not args.no_role_addresses,
            include_alt_domains=not args.no_alt_tlds,
            alt_tlds=alt_tlds,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    rows: list[dict] = []

    if args.no_verify:
        for c in candidates:
            rows.append({"email": c.email, "pattern": c.pattern, "tier": c.tier, "status": "not_checked"})
    else:
        from .verify import SMTPVerifier  # lazy: avoids requiring dnspython for --no-verify

        helo_domain = args.helo_domain or domain
        verifier = SMTPVerifier(
            helo_domain=helo_domain,
            mail_from=args.mail_from,
            timeout=args.timeout,
            delay=args.delay,
        )
        by_email = {c.email: c for c in candidates}
        for result in verifier.verify_many_grouped(
            [c.email for c in candidates],
            max_concurrent_domains=args.max_concurrent_domains,
            stop_on_valid=args.stop_on_valid,
        ):
            rows.append(
                {
                    "email": result.email,
                    "pattern": by_email[result.email].pattern,
                    "tier": by_email[result.email].tier,
                    "status": result.status,
                    "smtp_code": result.smtp_code,
                    "detail": result.detail or result.smtp_message,
                }
            )

        rows.sort(key=lambda r: STATUS_ORDER.get(r["status"], 99))

    _emit(rows, args.output, args.outfile)
    return 0


def _emit(rows: list[dict], fmt: str, outfile: str | None) -> None:
    out = open(outfile, "w", newline="") if outfile else sys.stdout
    try:
        if fmt == "json":
            json.dump(rows, out, indent=2)
            out.write("\n")
        elif fmt == "csv":
            if not rows:
                return
            writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        else:
            if not rows:
                print("No candidates generated.", file=out)
                return
            widths = {k: max(len(k), max(len(str(r.get(k, ""))) for r in rows)) for k in rows[0].keys()}
            header = "  ".join(k.ljust(widths[k]) for k in rows[0].keys())
            print(header, file=out)
            print("-" * len(header), file=out)
            for r in rows:
                print("  ".join(str(r.get(k, "")).ljust(widths[k]) for k in rows[0].keys()), file=out)
    finally:
        if outfile:
            out.close()


if __name__ == "__main__":
    raise SystemExit(main())
