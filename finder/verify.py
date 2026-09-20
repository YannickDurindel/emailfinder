"""SMTP-level email verification without sending any mail.

How it works: connect to the domain's mail server (MX record) and run
just enough of an SMTP conversation to ask "would you accept mail for
this address?" (EHLO -> MAIL FROM -> RCPT TO), then RSET/QUIT before
ever sending DATA. No message is transmitted.

Speed: all candidates that share a domain are checked over a *single*
reused SMTP session (MAIL FROM/RCPT TO/RSET repeated on one connection)
instead of reconnecting per address -- this is both faster (no repeated
TCP handshake + greeting per check) and more realistic/less alarming to
the mail server than opening many short-lived connections. Different
*domains* (e.g. the alternate-TLD long shots) are checked concurrently
against each other, since they're different mail servers -- concurrency
is capped and never exceeds one connection to any single server at a
time.

Caveats (read before trusting the results):
  - Many networks (residential ISPs, and most cloud VMs/containers) block
    outbound port 25. If every check comes back "error: connection
    refused/timed out", that's very likely why -- it says nothing about
    the addresses themselves.
  - Some mail hosts (e.g. IONOS/1&1) silently drop probing connections
    outright, regardless of network -- there's no workaround for that at
    this level.
  - Many providers (Gmail, Outlook/M365, and plenty of corporate servers)
    accept-then-bounce: they return 250 for almost anything at RCPT TO
    time and only reject during/after DATA, or silently drop it. That
    shows up here as "unknown" or a false "valid".
  - A domain can be "catch-all" (accepts RCPT TO for any local part). This
    tool detects that by probing a random, near-certainly-nonexistent
    address at the same domain; if it's also accepted, per-address
    results for that domain are marked catch_all and are not trustworthy.
  - Be a good citizen: this never opens more than one connection to the
    same mail server at a time, and paces requests within a session with
    `delay`. Don't run this against domains/targets you don't have a
    legitimate reason to be checking -- rapid-fire RCPT TO probing looks
    like directory harvesting to mail servers and can get your IP
    blocklisted.
"""
from __future__ import annotations

import queue
import random
import smtplib
import socket
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterator

try:
    import dns.resolver
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'dnspython'. Install with: pip install -r requirements.txt"
    ) from e


STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_UNKNOWN = "unknown"
STATUS_CATCH_ALL = "catch_all_domain"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

_RETRYABLE_CONNECT_ERRORS = (socket.timeout, TimeoutError, ConnectionRefusedError, smtplib.SMTPConnectError, OSError)


@dataclass
class VerifyResult:
    email: str
    status: str
    smtp_code: int | None = None
    smtp_message: str = ""
    mx_host: str = ""
    detail: str = ""


@dataclass
class _MXCache:
    cache: dict[str, list[str]] = field(default_factory=dict)

    def get(self, domain: str) -> list[str]:
        if domain not in self.cache:
            self.cache[domain] = _resolve_mx(domain)
        return self.cache[domain]


def _resolve_mx(domain: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(domain, "MX")
        hosts = sorted(
            ((r.preference, str(r.exchange).rstrip(".")) for r in answers),
            key=lambda x: x[0],
        )
        return [h for _, h in hosts]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        # some domains accept mail directly on their A record with no MX
        try:
            dns.resolver.resolve(domain, "A")
            return [domain]
        except Exception:
            return []
    except Exception:
        return []


def _random_local_part(length: int = 20) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _decode(message) -> str:
    return message.decode(errors="replace") if isinstance(message, bytes) else str(message)


class SMTPVerifier:
    def __init__(
        self,
        helo_domain: str = "example.com",
        mail_from: str | None = None,
        timeout: float = 10.0,
        delay: float = 1.0,
        max_reconnects: int = 3,
        reconnect_backoff: float = 3.0,
    ):
        self.helo_domain = helo_domain
        self.mail_from = mail_from or f"verify@{helo_domain}"
        self.timeout = timeout
        self.delay = delay
        # Some providers (notably Microsoft 365/Exchange Online Protection) let a
        # session run for a while, then cut it once they suspect directory
        # harvesting, rather than rejecting cleanly. On that kind of *mid-session*
        # disconnect (as opposed to a connection that never opens at all -- see
        # the IONOS case in the module docstring) we back off and reconnect
        # rather than giving up on the remaining candidates.
        self.max_reconnects = max_reconnects
        self.reconnect_backoff = reconnect_backoff
        self._mx = _MXCache()
        self._catch_all_cache: dict[str, bool | None] = {}

    # -- single-address convenience wrapper (opens its own short-lived session) --

    def verify(self, email: str) -> VerifyResult:
        if "@" not in email:
            return VerifyResult(email=email, status=STATUS_ERROR, detail="malformed address")
        domain = email.split("@", 1)[1]
        results = list(self.verify_domain_batch([email], domain))
        return results[0] if results else VerifyResult(email=email, status=STATUS_ERROR, detail="no result")

    # -- one reused session per domain, RSET between checks --

    def verify_domain_batch(
        self,
        emails: list[str],
        domain: str,
        stop_event: threading.Event | None = None,
    ) -> Iterator[VerifyResult]:
        if stop_event is not None and stop_event.is_set():
            for email in emails:
                yield VerifyResult(email=email, status=STATUS_SKIPPED, detail="stopped after a valid hit was found")
            return

        mx_hosts = self._mx.get(domain)
        if not mx_hosts:
            for email in emails:
                yield VerifyResult(email=email, status=STATUS_ERROR, detail="no MX/A record found for domain")
            return

        remaining = list(emails)
        last_error = ""
        # Work queue of hosts to try next. A mid-session disconnect re-queues
        # the *same* host (after a backoff wait) instead of giving up -- that's
        # a defensive server deciding to cut us off, not an unreachable one, so
        # waiting it out and reconnecting usually gets the rest through. A
        # connect-level failure (nothing ever answered) does NOT get re-queued;
        # retrying a host that's actively refusing/dropping every connection
        # wastes time for no benefit (see the IONOS case in the module docstring).
        host_queue = list(mx_hosts)
        reconnect_counts: dict[str, int] = {}

        while remaining and host_queue:
            mx_host = host_queue.pop(0)
            try:
                smtp = smtplib.SMTP(mx_host, 25, timeout=self.timeout)
                smtp.ehlo(self.helo_domain)
            except smtplib.SMTPServerDisconnected as e:
                last_error = f"{mx_host} disconnected before the session even started: {e}"
                continue
            except _RETRYABLE_CONNECT_ERRORS as e:
                last_error = f"could not connect to {mx_host}: {e}"
                continue

            try:
                catch_all = self._catch_all_cache.get(domain)
                if catch_all is None:
                    probe = f"{_random_local_part()}@{domain}"
                    try:
                        smtp.mail(self.mail_from)
                        code, _ = smtp.rcpt(probe)
                        smtp.rset()
                        catch_all = code in (250, 251)
                    except Exception:
                        catch_all = None
                    self._catch_all_cache[domain] = catch_all

                processed: list[str] = []
                disconnected = False
                for i, email in enumerate(remaining):
                    if stop_event is not None and stop_event.is_set():
                        break
                    if i > 0 and self.delay > 0:
                        time.sleep(self.delay)
                    try:
                        smtp.mail(self.mail_from)
                        code, message = smtp.rcpt(email)
                        smtp.rset()
                    except smtplib.SMTPServerDisconnected as e:
                        last_error = f"{mx_host} disconnected mid-session: {e}"
                        disconnected = True
                        break

                    message = _decode(message)
                    if code in (250, 251):
                        status = STATUS_CATCH_ALL if catch_all else STATUS_VALID
                        detail = "domain accepts mail for any address; this result is not conclusive" if catch_all else ""
                    elif code in (550, 551, 553):
                        status, detail = STATUS_INVALID, ""
                    else:
                        status, detail = STATUS_UNKNOWN, "server gave a non-definitive response (possibly greylisted)"

                    yield VerifyResult(email=email, status=status, smtp_code=code, smtp_message=message, mx_host=mx_host, detail=detail)
                    processed.append(email)

                try:
                    smtp.quit()
                except Exception:
                    pass

                remaining = [e for e in remaining if e not in processed]

                if stop_event is not None and stop_event.is_set() and remaining:
                    for email in remaining:
                        yield VerifyResult(email=email, status=STATUS_SKIPPED, detail="stopped after a valid hit was found")
                    remaining = []
                    return

                if disconnected and remaining:
                    tries = reconnect_counts.get(mx_host, 0) + 1
                    reconnect_counts[mx_host] = tries
                    if tries <= self.max_reconnects:
                        time.sleep(self.reconnect_backoff * tries)
                        host_queue.insert(0, mx_host)  # retry the same host after backing off
                    # else: give up on this host, fall through to any other MX host
                elif not remaining:
                    return
            except Exception as e:
                last_error = str(e)
                try:
                    smtp.quit()
                except Exception:
                    pass
                continue

        for email in remaining:
            yield VerifyResult(email=email, status=STATUS_ERROR, detail=last_error or "all MX hosts unreachable")

    # -- fan out across domains (never more than one live connection per domain) --

    @staticmethod
    def _group_by_domain(emails: list[str]) -> tuple[dict[str, list[str]], list[str]]:
        groups: dict[str, list[str]] = {}
        order: list[str] = []
        for email in emails:
            domain = email.rsplit("@", 1)[-1] if "@" in email else ""
            if domain not in groups:
                groups[domain] = []
                order.append(domain)
            groups[domain].append(email)
        return groups, order

    def verify_many_grouped(
        self,
        emails: list[str],
        max_concurrent_domains: int = 4,
        stop_on_valid: bool = False,
    ) -> Iterator[VerifyResult]:
        """Verify a list of emails, reusing one session per domain and running
        distinct domains concurrently (bounded by max_concurrent_domains).
        Yields results as they complete, not necessarily in input order.
        """
        groups, order = self._group_by_domain(emails)

        stop_event = threading.Event() if stop_on_valid else None

        if len(order) <= 1 or max_concurrent_domains <= 1:
            for domain in order:
                for result in self.verify_domain_batch(groups[domain], domain, stop_event=stop_event):
                    if stop_event is not None and result.status == STATUS_VALID:
                        stop_event.set()
                    yield result
            return

        q: "queue.Queue[VerifyResult | None]" = queue.Queue()

        def worker(domain: str) -> None:
            try:
                for result in self.verify_domain_batch(groups[domain], domain, stop_event=stop_event):
                    q.put(result)
            finally:
                q.put(None)

        with ThreadPoolExecutor(max_workers=max_concurrent_domains) as executor:
            for domain in order:
                executor.submit(worker, domain)

            remaining_workers = len(order)
            while remaining_workers > 0:
                item = q.get()
                if item is None:
                    remaining_workers -= 1
                    continue
                if stop_event is not None and item.status == STATUS_VALID:
                    stop_event.set()
                yield item
