# email-finder

Generates likely email addresses for a person given their name and company
domain, then (optionally) tests each candidate with a raw SMTP handshake
(`EHLO` → `MAIL FROM` → `RCPT TO` → `RSET`/`QUIT`) to see whether the mail
server accepts it — **no email is ever sent**, since `DATA` is never issued.

## Install

```bash
cd email-finder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Web UI (recommended)

```bash
python3 app.py
# then open http://127.0.0.1:5000
```

A local, single-user web app (binds to `127.0.0.1` only — not exposed to your
network). Fill in the name + domain, hit **Find emails**, and results stream
in live as each candidate is checked, with color-coded status badges and
CSV/JSON export. This is the same `finder` engine as the CLI underneath.

## CLI usage

```bash
# Generate + verify (default)
python3 -m finder.cli --first John --last Doe --domain example.com

# Just generate candidates, skip SMTP checks
python3 -m finder.cli --first John --last Doe --domain example.com --no-verify

# Stop as soon as one candidate is confirmed valid
python3 -m finder.cli --first Jane --last Smith --domain example.com --stop-on-valid

# CSV output to a file
python3 -m finder.cli --first Jane --last Smith --domain example.com --output csv --outfile results.csv

# Only know the company name, not the domain (unreliable guess, use with caution)
python3 -m finder.cli --first Jane --last Smith --company "Example Inc" --output json
```

### Key flags

| Flag | Purpose |
|---|---|
| `--domain` | The company's real email domain (preferred over `--company`). |
| `--company` | Falls back to guessing `<slug>.com` from the company name — confirm manually. |
| `--delay` | Seconds between SMTP probes (default 1.5). Don't set this to 0. |
| `--stop-on-valid` | Stop at the first confirmed hit instead of checking every pattern. |
| `--helo-domain` | Domain to present in `EHLO`/`MAIL FROM`. Defaults to the target domain. |
| `--no-role-addresses` | Skip generic mailboxes (`contact@`, `sales@`, …). Included by default. |
| `--no-alt-tlds` | Skip long-shot alternate-TLD guesses. Included by default. |
| `--alt-tlds` | Comma-separated TLDs to try, e.g. `fr,co,us` (default). |
| `--output` | `table` (default), `csv`, or `json`. |

## Candidates generated, in tiers (checked in this order)

1. **`personal`** — 16 name-pattern guesses at the given domain: `first.last`,
   `firstlast`, `f.last`, `flast`, `first`, `last`, `first_last`, `first-last`,
   `last.first`, `lastfirst`, `last_first`, `l.first`, `lfirst`, `firstl`,
   `first.l`, `last-first`. These are the most likely to be right.
2. **`role`** — generic mailboxes at the same domain: `contact@`, `info@`,
   `sales@`, `support@`, `hello@`, `admin@`, `office@`. Not tied to the
   person, but often a legitimate way to reach the company.
3. **`alt-domain`** — long shots at alternate-TLD versions of the same
   company name (default `.fr`, `.co`, `.us` — configurable), in case the
   real domain isn't the one you assumed. Only the two most common name
   patterns (`first.last`, `flast`) plus the generic mailboxes are tried per
   alternate domain, to keep the candidate count sane.

`--stop-on-valid` respects this order, so it stops on the first (most
likely) hit rather than an early long shot.

## Result statuses

- **valid** — server returned 250/251 for `RCPT TO`, and the domain does not
  appear to be catch-all.
- **catch_all_domain** — the domain accepts *any* address (verified by
  probing a random nonexistent mailbox). Per-address results here are not
  trustworthy — treat all its candidates as "unknown".
- **invalid** — server explicitly rejected the address (550/551/553).
- **unknown** — a non-definitive response (e.g. 4xx greylisting).
- **error** — couldn't complete the check (DNS failure, connection refused,
  timeout, etc.) — says nothing about the address itself.

## Speed

Candidates on the same domain share a single reused SMTP session (`RSET`
between checks) instead of reconnecting for every address, and different
domains (e.g. the alt-TLD long shots) are checked concurrently against each
other — capped by `--max-concurrent-domains` (CLI, default 4) or "Max
parallel domains" (web UI). This never opens more than one connection to
any single mail server at a time, so it's not "hammering" any one server
any harder than checking one address would — it's just not making N
distinct domains wait in line behind each other.

## Important limitations — read before relying on results

- **Port 25 is commonly blocked.** Most residential ISPs and most cloud
  providers/VMs block outbound port 25 by default. If everything comes back
  `error: connection refused` or `timeout`, that's almost certainly why —
  try from a network/host where outbound 25 is actually open, or use a
  provider-specific API (see below) instead.
- **Some mail hosts silently drop probing connections outright**, regardless
  of your network. IONOS/1&1 (`mx*.ionos.fr`/`.de`/`.com`) is a known example
  — it doesn't reject the handshake, it just never responds, so every
  candidate at an IONOS-hosted domain will time out no matter where you run
  this from. There's no workaround at the SMTP-probing level for a domain
  that behaves this way; a paid verification API (below) may have better luck
  since it may connect from IP ranges those providers already trust.
- **Accept-then-bounce providers.** Gmail, Microsoft 365, and many
  well-defended corporate servers return 250 at `RCPT TO` for nearly
  anything and only reject later (or silently drop the message). For those
  domains, SMTP-level probing is unreliable — the tool will mark them
  `catch_all_domain` or `unknown` when it detects this, but treat any
  "valid" result for a major provider's domain with real skepticism.
- **This is a fingerprinting technique, not a guarantee.** Treat "valid" as
  "plausible, worth trying," not "confirmed to reach a human."
- **Be a good citizen / stay legal.** Rapid RCPT TO probing looks like
  directory-harvesting to mail servers and can get the source IP
  blocklisted. Keep `--delay` reasonable, don't parallelize probes against
  the same domain, and only use this against companies/domains where you
  have a legitimate reason to be doing outreach. Once you do have a
  validated address, sending unsolicited commercial email is still subject
  to laws like CAN-SPAM (US) and GDPR/PECR (EU/UK) — make sure whatever you
  send with the result complies.

## Alternative when port 25 is blocked

If SMTP probing isn't viable on your network, a paid verification API
(NeverBounce, ZeroBounce, Hunter.io, etc.) does the same handshake from
infrastructure with open port 25 outbound, over HTTPS. Not included here
since it requires a paid API key.
