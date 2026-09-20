"""Local web UI for email-finder.

Run with: python3 app.py
Then open http://127.0.0.1:5000

This binds to localhost only. The SMTP verification it performs still goes
out over the network to whatever mail server you target -- see README.md
for the same caveats (port 25 may be blocked on your network, catch-all
domains, accept-then-bounce providers, and please be respectful of the
domains you probe).
"""
from __future__ import annotations

import json

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from finder.patterns import DEFAULT_ALT_TLDS, generate_full_candidates, guess_domain_from_company

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


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/search")
def api_search():
    first = request.args.get("first", "").strip()
    last = request.args.get("last", "").strip()
    domain = request.args.get("domain", "").strip().lower()
    do_verify = request.args.get("verify", "1") != "0"
    stop_on_valid = request.args.get("stop_on_valid", "0") == "1"
    delay = max(0.0, min(float(request.args.get("delay", 1.5) or 1.5), 10.0))
    timeout = max(1.0, min(float(request.args.get("timeout", 10) or 10), 30.0))
    helo_domain = request.args.get("helo_domain", "").strip() or domain
    include_roles = request.args.get("include_roles", "1") != "0"
    include_alt_tlds = request.args.get("include_alt_tlds", "1") != "0"
    alt_tlds_raw = request.args.get("alt_tlds", ",".join(DEFAULT_ALT_TLDS))
    alt_tlds = tuple(t.strip() for t in alt_tlds_raw.split(",") if t.strip())
    max_concurrent_domains = max(1, min(int(request.args.get("max_concurrent_domains", 4) or 4), 8))

    try:
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
    print("email-finder web UI: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
