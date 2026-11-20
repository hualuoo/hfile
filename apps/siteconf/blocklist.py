"""访问控制: the IP and User-Agent blocklists ("IP 黑名单" and "UA 黑名单", inventory 11.11).

One rule per line; a line starting with # is a comment.

- IP: an IPv4 or IPv6 address, or a network in CIDR (192.168.0.0/24, 2001:db8::/32). An address with
  host bits after the slash (192.168.0.7/24) means its network. IPv4-mapped IPv6 rules
  (::ffff:1.2.3.4) are their IPv4 address: apps.core.net gives IPv4 clients as IPv4.
- UA: the whole User-Agent, where * stands for any characters (none too). Letters keep their case
  ("*Thunder*" is not "*thunder*"). A request without a User-Agent never matches; one
  longer than MAX_USER_AGENT matches whenever there are UA rules. Rules in other scripts work: the
  header is read as UTF-8 (user_agent()).

parse() checks the text and says what is wrong on which line (the settings page, serializers.py);
compiled() is what the middleware matches requests with: parsed once per text and kept, so a saved
change (a new text) is parsed again at the next request.
"""

from __future__ import annotations

import functools
import ipaddress
from dataclasses import dataclass, field

from django.utils.translation import gettext as _, ngettext

MAX_IP_LINES = 2000
MAX_UA_LINES = 500
MAX_LINE = 500  # characters
SHOWN_LINES = 20  # bad lines an error names; the rest are counted
# Real User-Agents are a few hundred characters. A longer one isn't matched rule by rule (500 rules
# against 256 KB, what waitress accepts, cost 40 ms a request): while there are UA rules, it is blocked.
MAX_USER_AGENT = 4096

# What "屏蔽迅雷下载" adds to the UA blocklist (Thunder's download service).
THUNDER_RULES = (
    "*Thunder*",
    "Mozilla/5.0 (Linux; Android) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/34.0.1847.131 Safari/537.36",
    "*Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1;*",
)

_MAPPED = ipaddress.ip_network("::ffff:0:0/96")


class RuleError(ValueError):
    pass


def shown(problems):
    """The messages of the first SHOWN_LINES bad lines, then how many more there are. Every one was named,
    and the settings page checks the whole list as the admin types: up to 11 MB an answer (SEC-06)."""
    if len(problems) <= SHOWN_LINES:
        return problems
    return [*problems[:SHOWN_LINES], ngettext("%(n)s more line is wrong too.", "%(n)s more lines are wrong too.", len(problems) - SHOWN_LINES) % {"n": len(problems) - SHOWN_LINES}]


def _lines(text):
    """(line number, rule) of every rule line: blank lines and # comments left out."""
    for number, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        rule = line.strip()
        if rule and not rule.startswith("#"):
            yield number, rule


def ip_network(rule):
    """The network a rule of the IP blocklist means; raises RuleError."""
    if "%" in rule:
        raise RuleError(_("leave the % and the interface name off an IPv6 address"))
    try:
        network = ipaddress.ip_network(rule, strict=False)
    except ValueError:
        raise RuleError(_("not an IP address or a CIDR range (like 192.168.0.0/24 or 2001:db8::/32)")) from None
    if network.version == 6 and network.subnet_of(_MAPPED):
        network = ipaddress.ip_network(f"{network.network_address.ipv4_mapped}/{network.prefixlen - 96}")
    return network


def _check_ua(rule):
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in rule):
        raise RuleError(_("can't contain control characters"))


@dataclass
class Parsed:
    """The rules of one text, ready to match; problems are (line number, message) of bad lines."""

    # (version, prefix length) -> {network address as an int}: one set lookup per prefix length.
    networks: dict = field(default_factory=dict)
    ip_rules: dict = field(default_factory=dict)  # (version, prefix length, int) -> the rule as written
    ua_rules: list = field(default_factory=list)  # (the rule, its parts between the *s)
    problems: list = field(default_factory=list)

    def ip_rule(self, ip):
        """The rule that blocks this IP (text, as apps.core.net gives it), or None."""
        if not ip or not self.networks:
            return None
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return None
        value, bits = int(address), address.max_prefixlen
        for (version, length), starts in self.networks.items():
            if version == address.version:
                start = value >> (bits - length) << (bits - length) if length else 0
                if start in starts:
                    return self.ip_rules[(version, length, start)]
        return None

    def ua_rule(self, user_agent):
        """The rule that blocks this User-Agent, or None."""
        if not user_agent:
            return None
        if len(user_agent) > MAX_USER_AGENT and self.ua_rules:
            return ngettext("(User-Agent over %(limit)s character)", "(User-Agent over %(limit)s characters)", MAX_USER_AGENT) % {"limit": MAX_USER_AGENT}
        for rule, parts in self.ua_rules:
            if _glob_match(parts, user_agent):
                return rule
        return None

    def blocks(self, ip, user_agent):
        return self.ip_rule(ip) is not None or self.ua_rule(user_agent) is not None


def user_agent(meta):
    """The User-Agent of a request's META as text: WSGI hands headers over as latin-1, browsers and
    tools send UTF-8."""
    raw = meta.get("HTTP_USER_AGENT", "")
    try:
        return raw.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return raw


def _glob_match(parts, text):
    """Whether `text` is matched as a whole by the rule split at its *s. Linear: the first part must
    begin the text, the last end it, and those between appear in order (the leftmost place each one
    fits is always right when * is the only wildcard)."""
    if len(parts) == 1:
        return text == parts[0]
    first, last = parts[0], parts[-1]
    if len(text) < len(first) + len(last) or not text.startswith(first) or not text.endswith(last):
        return False
    position, end = len(first), len(text) - len(last)
    for part in parts[1:-1]:
        if part:
            found = text.find(part, position, end)
            if found < 0:
                return False
            position = found + len(part)
    return True


def parse(ip_text="", ua_text=""):
    """Both lists parsed; Parsed.problems lists ("ip" or "ua", line, message) of the lines that aren't
    rules. Bad lines are left out of the rest."""
    parsed = Parsed()
    for number, rule in _lines(ip_text or ""):
        try:
            if len(rule) > MAX_LINE:
                raise RuleError(ngettext("too long (at most %(limit)s character)", "too long (at most %(limit)s characters)", MAX_LINE) % {"limit": MAX_LINE})
            network = ip_network(rule)
        except RuleError as error:
            parsed.problems.append(("ip", number, str(error)))
            continue
        key = (network.version, network.prefixlen)
        start = int(network.network_address)
        parsed.networks.setdefault(key, set()).add(start)
        parsed.ip_rules.setdefault((*key, start), rule)
    for number, rule in _lines(ua_text or ""):
        try:
            if len(rule) > MAX_LINE:
                raise RuleError(ngettext("too long (at most %(limit)s character)", "too long (at most %(limit)s characters)", MAX_LINE) % {"limit": MAX_LINE})
            _check_ua(rule)
        except RuleError as error:
            parsed.problems.append(("ua", number, str(error)))
            continue
        parsed.ua_rules.append((rule, rule.split("*")))
    return parsed


@functools.lru_cache(maxsize=4)
def compiled(ip_text, ua_text):
    """parse(), kept per text: for every request (apps/siteconf/middleware.py)."""
    return parse(ip_text, ua_text)


def normalized_ip_text(text):
    """The IP list as it is saved: lines trimmed, blank lines and repeated rules dropped, comments
    kept. Raises RuleError naming the bad lines (shown())."""
    kept, seen, problems = [], set(), []
    for number, line in enumerate((text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        rule = line.strip()
        if not rule:
            continue
        if rule.startswith("#"):
            kept.append(rule)
            continue
        try:
            if len(rule) > MAX_LINE:
                raise RuleError(ngettext("too long (at most %(limit)s character)", "too long (at most %(limit)s characters)", MAX_LINE) % {"limit": MAX_LINE})
            network = ip_network(rule)
        except RuleError as error:
            problems.append(_('Line %(number)s, "%(rule)s": %(error)s') % {"number": number, "rule": rule[:60], "error": error})
            continue
        written = str(network.network_address) if network.prefixlen == network.max_prefixlen else str(network)
        if written not in seen:
            seen.add(written)
            kept.append(written)
    problems = shown(problems)
    _limit(kept, MAX_IP_LINES, problems)
    if problems:
        raise RuleError("\n".join(problems))
    return "\n".join(kept)


def normalized_ua_text(text):
    """The UA list as it is saved: lines trimmed, blank lines and repeated rules dropped."""
    kept, seen, problems = [], set(), []
    for number, line in enumerate((text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        rule = line.strip()
        if not rule:
            continue
        if not rule.startswith("#"):
            try:
                if len(rule) > MAX_LINE:
                    raise RuleError(ngettext("too long (at most %(limit)s character)", "too long (at most %(limit)s characters)", MAX_LINE) % {"limit": MAX_LINE})
                _check_ua(rule)
            except RuleError as error:
                problems.append(_("Line %(number)s: %(error)s") % {"number": number, "error": error})
                continue
            if rule in seen:
                continue
            seen.add(rule)
        kept.append(rule)
    problems = shown(problems)
    _limit(kept, MAX_UA_LINES, problems)
    if problems:
        raise RuleError("\n".join(problems))
    return "\n".join(kept)


def _limit(kept, most, problems):
    rules = sum(1 for line in kept if not line.startswith("#"))
    if rules > most:
        problems.append(ngettext("At most %(limit)s rule; there are %(count)s now.", "At most %(limit)s rules; there are %(count)s now.", most) % {"limit": most, "count": rules})
