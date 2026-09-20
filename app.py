"""Local web UI for email-finder.

Run with: python3 app.py
Then open http://127.0.0.1:5000 (or, if EMAIL_FINDER_HOST=0.0.0.0, from
another device on your Tailscale network / LAN -- see README.md).

The SMTP verification this performs goes out over the network to whatever
mail server you target -- see README.md for the same caveats (port 25 may
be blocked on your network, catch-all domains, accept-then-bounce
providers, and please be respectful of the domains you probe).

No authentication: anything that can reach this server can trigger a
search (and the live SMTP probes that come with it). Fine for
127.0.0.1-only use; if you bind it beyond localhost, only do so on a
network you trust.
"""
from __future__ import annotations

import json
import os

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from finder.patterns import (
    DEFAULT_ALT_TLDS,
    DEFAULT_DISCOVERY_TLDS,
    generate_candidates_for_domains,
    generate_domain_guesses,
    generate_full_candidates,
    guess_domain_from_company,
)

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/guess-domain")
def api_guess_domain():
    company = request.args.get("company", "").strip()
    tld = request.args.get("tld", "com").strip() or "com"
    if not company:
        return jsonify({"domain": ""})
    return jsonify({"domain": guess_domain_from_company(company, tld=tld)})


@app.get("/api/discover-domains")
def api_discover_domains():
    """DNS-only check (no SMTP contact) of which common-TLD variants of a
    company name actually exist, so the caller can run the expensive SMTP
    verification only against domains confirmed to be real.
    """
    company = request.args.get("company", "").strip()
    tlds_raw = request.args.get("tlds", ",".join(DEFAULT_DISCOVERY_TLDS))
    tlds = tuple(t.strip() for t in tlds_raw.split(",") if t.strip())
    if not company:
        return jsonify({"domains": [], "checked": []})

    from finder.verify import discover_domains  # lazy import: dnspython not needed elsewhere

    candidates = generate_domain_guesses(company, tlds=tlds)
    found = discover_domains(candidates)
    return jsonify(
        {
            "domains": [{"domain": d.domain, "has_mx": d.has_mx} for d in found],
            "checked": candidates,
        }
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/search")
def api_search():
    first = request.args.get("first", "").strip()
    last = request.args.get("last", "").strip()
    domain = request.args.get("domain", "").strip().lower()
    domains_raw = request.args.get("domains", "").strip()
    domains_list = [d.strip().lower() for d in domains_raw.split(",") if d.strip()]
    do_verify = request.args.get("verify", "1") != "0"
    stop_on_valid = request.args.get("stop_on_valid", "0") == "1"
    delay = max(0.0, min(float(request.args.get("delay", 1.5) or 1.5), 10.0))
    timeout = max(1.0, min(float(request.args.get("timeout", 10) or 10), 30.0))
    helo_domain = request.args.get("helo_domain", "").strip() or domain or (domains_list[0] if domains_list else "example.com")
    include_roles = request.args.get("include_roles", "1") != "0"
    include_alt_tlds = request.args.get("include_alt_tlds", "1") != "0"
    alt_tlds_raw = request.args.get("alt_tlds", ",".join(DEFAULT_ALT_TLDS))
    alt_tlds = tuple(t.strip() for t in alt_tlds_raw.split(",") if t.strip())
    max_concurrent_domains = max(1, min(int(request.args.get("max_concurrent_domains", 4) or 4), 8))

    try:
        if domains_list:
            # One or more domains already confirmed to exist (see
            # /api/discover-domains) -- run the full pattern set against each,
            # no further alt-TLD long-shot guessing needed.
            candidates = generate_candidates_for_domains(first, last, domains_list, include_roles=include_roles)
        else:
            candidates = generate_full_candidates(
                first,
                last,
                domain,
                include_roles=include_roles,
                include_alt_domains=include_alt_tlds,
                alt_tlds=alt_tlds,
            )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    def stream():
        yield _sse(
            "candidates",
            {"candidates": [{"email": c.email, "pattern": c.pattern, "tier": c.tier} for c in candidates]},
        )

        if not do_verify:
            yield _sse("done", {"verified": False})
            return

        from finder.verify import SMTPVerifier  # lazy import: dnspython not needed for --no-verify path

        verifier = SMTPVerifier(helo_domain=helo_domain, timeout=timeout, delay=delay)
        for result in verifier.verify_many_grouped(
            [c.email for c in candidates],
            max_concurrent_domains=max_concurrent_domains,
            stop_on_valid=stop_on_valid,
        ):
            yield _sse(
                "result",
                {
                    "email": result.email,
                    "status": result.status,
                    "smtp_code": result.smtp_code,
                    "detail": result.detail or result.smtp_message,
                    "mx_host": result.mx_host,
                },
            )

        yield _sse("done", {"verified": True})

    return Response(stream_with_context(stream()), mimetype="text/event-stream")


if __name__ == "__main__":
    host = os.environ.get("EMAIL_FINDER_HOST", "127.0.0.1")
    port = int(os.environ.get("EMAIL_FINDER_PORT", "5000"))

    print(f"email-finder web UI: http://{host}:{port}")
    if host != "127.0.0.1":
        print("  NOTE: bound to a non-localhost address -- reachable by anything that can route to it on this network.")

    app.run(host=host, port=port, debug=False, threaded=True)
