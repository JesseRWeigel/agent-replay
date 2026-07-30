"""Redaction. This is the part that must not be wrong.

The whole point of this tool is that a run can be posted publicly, so a redaction miss is not
a cosmetic bug, it publishes someone's key. Everything here is built around that asymmetry:
over-redacting costs a reader some context, under-redacting costs the author their credentials.
When the two conflict, redact.

Three mechanisms, deliberately layered, because each one misses things the others catch.

1. KNOWN FORMATS. Provider tokens have recognisable shapes, and matching them exactly is
   precise and cheap. This catches most real leaks and never fires on prose.

2. ASSIGNMENT CONTEXT. A value is suspicious because of what it is called, regardless of what
   it looks like. `PASSWORD=hunter2` matches no key format at all. This is what catches the
   long tail of homegrown secrets, `.env` contents and config dumps.

3. HIGH ENTROPY IN A SECRET CONTEXT. A long random-looking string near a secret-ish word is
   redacted even when it matches no known format, because tomorrow's provider will invent a
   prefix this file has never heard of.

The verification is deliberately separated from all of it. `find_leaks()` is written
independently of the redactors above, against the produced output rather than the input, so a
bug in a redactor has to be matched by an identical bug in the checker before anything escapes.
A checker sharing code with the thing it checks cannot catch that code's mistakes.
"""
from __future__ import annotations

import base64
import binascii
import math
import os
import re
from dataclasses import dataclass, field

PLACEHOLDER = "[redacted:{kind}]"

# ---------------------------------------------------------------- mechanism 1: known formats
#
# Ordered most specific first. Each carries a name that appears in the placeholder, so a reader
# can tell a redacted AWS key from a redacted GitHub token without seeing either.
KNOWN = [
    ("anthropic-key",  re.compile(r"sk-ant-(?:api\d\d-)?[A-Za-z0-9_\-]{24,}")),
    ("openrouter-key", re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}")),
    ("openai-key",     re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("github-pat",     re.compile(r"github_pat_[A-Za-z0-9_]{50,}")),
    ("github-token",   re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("gitlab-token",   re.compile(r"glpat-[A-Za-z0-9_\-]{16,}")),
    ("google-key",     re.compile(r"AIza[A-Za-z0-9_\-]{35}")),
    ("aws-access-key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}(?![A-Z0-9])")),
    ("slack-token",    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("slack-webhook",  re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/+]{20,}")),
    ("discord-webhook", re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w\-]{20,}")),
    ("stripe-key",     re.compile(r"[rs]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("npm-token",      re.compile(r"npm_[A-Za-z0-9]{36}")),
    ("pypi-token",     re.compile(r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{50,}")),
    ("huggingface-token", re.compile(r"hf_[A-Za-z0-9]{30,}")),
    ("sendgrid-key",   re.compile(r"SG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}")),
    ("twilio-sid",     re.compile(r"AC[0-9a-fA-F]{32}")),
    # Whole block first, then a header-plus-body form with no END marker. The second exists
    # because a TRUNCATED key is still a key: a transcript that captured the first 60 lines of
    # an id_rsa has published enough to matter. Requiring END meant the redactor missed those
    # while the independent checker caught them, and the two disagreeing is precisely what the
    # separation is meant to expose. The checker was right, so the redactor was raised to meet
    # it rather than the check being relaxed.
    ("private-key",    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
        r"[\s\S]*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")),
    ("private-key-truncated", re.compile(
        r"-{0,5}BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-{0,5}"
        r"(?:[\s\\rn\"']|-----){0,12}"
        r"(?=[A-Za-z0-9+/]{40,})(?=[A-Za-z0-9+/]*[G-Zg-z+/])[A-Za-z0-9+/=\s\\rn]{40,}")),
    ("jwt",            re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer-token",   re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.=]{20,}")),
    ("basic-auth",     re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/]{20,}={0,2}")),
    # A connection string with an inline password. Only the password is replaced, so the reader
    # still sees which service and which host was involved.
    ("db-password",    re.compile(
        r"(?i)\b((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|ftp)://[^\s:@/]+:)"
        r"([^\s@/]{3,})(@)")),
]

# ---------------------------------------------------------- mechanism 2: assignment context
#
# Names that make whatever follows them a secret. Deliberately broad, since a false positive
# here costs a reader one obscured value.
SECRET_WORD = (
    r"(?:api[_\-]?key|apikey|secret|passwd|password|passphrase|token|auth|credential|"
    r"private[_\-]?key|access[_\-]?key|client[_\-]?secret|session[_\-]?key|encryption[_\-]?key|"
    r"signing[_\-]?key|webhook[_\-]?url|connection[_\-]?string|dsn|bearer|cookie|"
    r"refresh[_\-]?token|id[_\-]?token|sas[_\-]?token|account[_\-]?key)"
)

# KEY=value, KEY: value, "key": "value", --key value, -H 'X-Api-Key: value'
ASSIGNMENTS = [
    # shell and dotenv: FOO_TOKEN=value, optionally quoted, up to end of line
    re.compile(rf"(?im)^(\s*(?:export\s+)?[A-Z0-9_]*{SECRET_WORD}[A-Z0-9_]*\s*=\s*)"
               rf"(['\"]?)([^\s'\"#][^\n'\"#]*)(\2)"),
    # json / yaml: "apiKey": "value"
    re.compile(rf"(?i)([\"']?[A-Za-z0-9_\-]*{SECRET_WORD}[A-Za-z0-9_\-]*[\"']?\s*[:=]\s*)"
               rf"([\"'])([^\"'\n]{{4,}})([\"'])"),
    # cli flags: --api-key value  /  --token=value
    re.compile(rf"(?i)(--[A-Za-z0-9\-]*{SECRET_WORD}[A-Za-z0-9\-]*[=\s]+)"
               rf"([\"']?)([^\s\"'&;|]{{6,}})(\2)"),
    # headers: Authorization: <value> / X-Api-Key: <value>
    re.compile(rf"(?im)^(\s*(?:-H\s+['\"]?)?(?:Authorization|X-[A-Za-z\-]*{SECRET_WORD}[A-Za-z\-]*)"
               rf"\s*:\s*)([^\n'\"]{{8,}})()()"),
]

# --------------------------------------------------- mechanism 3: entropy in a secret context
ENTROPY_WINDOW = 90          # characters either side of a secret-ish word to search
ENTROPY_MIN_LEN = 20
ENTROPY_THRESHOLD = 3.6      # bits per character; english prose sits well below this
SECRET_NEARBY = re.compile(SECRET_WORD, re.I)
# Deliberately excludes '=' so a candidate cannot span an assignment and swallow the variable
# name: `DEBUG_TOKEN_PARSER=true` was being redacted whole, because the combined string is long
# and looks random even though the value is the word "true".
CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-]{20,}={0,2}")

# Things that look high entropy and are not secrets. Without this the tool redacts every git
# SHA and content hash in a transcript, which destroys its usefulness for reading a run.
BENIGN = re.compile(
    r"^(?:[0-9a-f]{7,40}|[0-9A-F]{7,40})$"          # git sha / hex digest
    r"|^(?:sha256|sha1|md5|blake3)[-:]"
    r"|^[A-Za-z0-9_\-]+\.(?:js|ts|tsx|jsx|py|md|json|html|css|lock|txt|yml|yaml|toml)$"
    r"|^(?:https?|file|data):"
    # A SCREAMING_SNAKE identifier is a NAME, not a value. Redacting it hides which setting was
    # involved while protecting nothing, since the value is matched separately.
    r"|^[A-Z][A-Z0-9_]{3,}$"
    # Ordinary code identifiers, dotted paths and kebab slugs.
    r"|^[a-z][a-zA-Z0-9]*(?:[._\-][a-zA-Z0-9]+)+$"
)

# A candidate that is itself a secret-ish word is a label rather than a credential.
LOOKS_LIKE_NAME = re.compile(SECRET_WORD, re.I)

# The entropy pass additionally requires a digit. `authenticateUserWithProvider` is long,
# mixed-case, and contains "auth", so it scored as a secret and got redacted, which would blank
# out identifiers all through a transcript. Real credentials essentially always mix digits in.
# The cost is a purely alphabetic secret slipping past THIS mechanism; it is still caught by the
# known-format and assignment passes, which is what the layering is for. Stated in the README.
HAS_DIGIT = re.compile(r"[0-9]")


def shannon(s: str) -> float:
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


@dataclass
class Redaction:
    kind: str
    count: int = 0
    samples: list = field(default_factory=list)   # first bytes only, for the report


@dataclass
class Result:
    text: str
    redactions: dict           # kind -> Redaction
    total: int = 0


def _placeholder(kind: str) -> str:
    return PLACEHOLDER.format(kind=kind)


def redact(text: str, *, home: str | None = None, extra_patterns=None) -> Result:
    """Redact secrets from a string.

    `home` replaces the user's home directory with `~`, because an absolute path carries a
    username and a machine layout that most people do not intend to publish.
    """
    if not isinstance(text, str) or not text:
        return Result(text if isinstance(text, str) else "", {}, 0)

    found: dict[str, Redaction] = {}

    def note(kind: str, matched: str) -> str:
        r = found.setdefault(kind, Redaction(kind))
        r.count += 1
        if len(r.samples) < 3:
            # Only a shape hint, never the value: length and first two characters.
            r.samples.append(f"{matched[:2]}… ({len(matched)} chars)")
        return _placeholder(kind)

    out = text

    # 1. Known formats.
    for kind, rx in KNOWN:
        if kind == "db-password":
            out = rx.sub(lambda m: m.group(1) + note(kind, m.group(2)) + m.group(3), out)
        else:
            out = rx.sub(lambda m, k=kind: note(k, m.group(0)), out)

    for kind, rx in (extra_patterns or []):
        out = rx.sub(lambda m, k=kind: note(k, m.group(0)), out)

    # 2. Assignment context.
    for rx in ASSIGNMENTS:
        def repl(m):
            groups = m.groups()
            prefix = groups[0]
            # The value group differs per pattern; take the last non-empty capture before the
            # optional closing quote.
            if len(groups) >= 4:
                q1, value, q2 = groups[1], groups[2], groups[3]
            else:
                q1, value, q2 = "", groups[1], ""
            if not value or value.strip() in ("", "true", "false", "null", "none", "undefined"):
                return m.group(0)
            if value.startswith("[redacted:"):
                return m.group(0)
            # A reference to another variable is not a secret: KEY=$OTHER, key: ${env.X}
            if re.fullmatch(r"[\$\{][^\s]*", value.strip()):
                return m.group(0)
            return f"{prefix}{q1}{note('assigned-secret', value)}{q2}"
        out = rx.sub(repl, out)

    # 3. Entropy near a secret word.
    for m in SECRET_NEARBY.finditer(out):
        lo = max(0, m.start() - ENTROPY_WINDOW)
        hi = min(len(out), m.end() + ENTROPY_WINDOW)
        window = out[lo:hi]
        for c in CANDIDATE.finditer(window):
            tok = c.group(0)
            if len(tok) < ENTROPY_MIN_LEN or BENIGN.search(tok):
                continue
            if tok.startswith("[redacted:") or "redacted" in tok:
                continue
            if LOOKS_LIKE_NAME.fullmatch(tok):
                continue
            if not HAS_DIGIT.search(tok):
                continue
            if shannon(tok) < ENTROPY_THRESHOLD:
                continue
            out = out.replace(tok, note("high-entropy", tok))

    # 4a. This machine's home becomes a plain ~, since that is the reader's own layout and the
    # most readable substitution. Done FIRST, so the generic pass below does not turn it into
    # the anonymous form.
    if home:
        for variant in (home, home.replace("\\", "/")):
            if variant and variant != "/" and variant in out:
                n = out.count(variant)
                out = out.replace(variant, "~")
                r = found.setdefault("home-path", Redaction("home-path"))
                r.count += n

    # 4b. Any OTHER home-shaped path. A transcript quotes paths from documentation, from other
    # people's machines and from code samples, and each one carries a username. Redacting only
    # the local $HOME left those in place, the independent checker flagged them, and the
    # checker was right. The trailing boundary allows a path with no trailing slash, since
    # `cd /home/carol` discloses exactly as much as `/home/carol/`.
    # Three real shapes, because one pattern could not cover them without also matching
    # ordinary relative paths like ./src/home/user/x.ts:
    #   /home/name or /Users/name at a token boundary
    #   /mnt/c/Users/name, which is how WSL exposes a Windows profile and is extremely common
    #     in these transcripts. A single lookbehind rejected it, since the preceding character
    #     is the drive letter, a word character.
    #   C:\Users\name, the native Windows form
    HOME_SHAPES = [
        re.compile(r"(?:(?<=^)|(?<=[\s\"\'`=:,(\[]))(/home|/Users)/"
                   r"([A-Za-z0-9][A-Za-z0-9._\-]{1,31})", re.M),
        re.compile(r"(/mnt/[a-z]/Users)/([A-Za-z0-9][A-Za-z0-9._\-]{1,31})", re.I),
        re.compile(r"([A-Za-z]:[\\/]{1,2}Users)[\\/]{1,2}([A-Za-z0-9][A-Za-z0-9._\-]{1,31})"),
    ]

    def _anon_home(m):
        note("home-path", m.group(0))
        return f"{m.group(1)}/~user"
    for rx in HOME_SHAPES:
        out = rx.sub(_anon_home, out)

    total = sum(r.count for r in found.values())
    return Result(out, found, total)


# ------------------------------------------------------------------- independent leak checker
#
# Written from scratch, against the OUTPUT. It deliberately shares no regex with the redactors
# above: if it reused them, a bug in a pattern would be invisible to the check that is supposed
# to catch that bug. It is broader and noisier than the redactors on purpose, since its job is
# to refuse a publish, not to produce readable output.
LEAK_CHECK = [
    ("provider token", re.compile(
        r"\b(?:sk|pk|rk)[-_](?:ant|or|proj|live|test)?[-_]?[A-Za-z0-9]{20,}")),
    ("github credential", re.compile(r"\bgh[a-z]_[A-Za-z0-9]{25,}|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("aws identifier", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{14,}")),
    ("google credential", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("bearer header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{25,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.")),
    # A PEM header ALONE is not key material. A transcript that greps for
    # "BEGIN OPENSSH PRIVATE KEY", or that contains this very file's regex, matches the phrase
    # while carrying no key at all, and refusing to publish over it is a false alarm that
    # teaches people to pass --allow-leaks. Real leaks, whole or truncated, are followed by
    # base64 body, so the body is what is required. Proven both ways in the tests.
    ("pem block", re.compile(
        # Body must follow the header across whitespace or JSON escapes only, and must look
        # like base64 rather than hex. A 200-character window reached past the header, through
        # unrelated JSON, and landed on a 40-hex-digit git SHA, which is not key material.
        # The lookahead demands at least one character outside the hex alphabet.
        r"BEGIN[ A-Z]*PRIVATE KEY[ A-Z]*-{0,5}"
        r"(?:[\s\\rn\"']|-----){0,12}"
        r"(?=[A-Za-z0-9+/]{40,})(?=[A-Za-z0-9+/]*[G-Zg-z+/])[A-Za-z0-9+/]{40,}")),
    ("slack token", re.compile(r"\bxox[a-z]-[A-Za-z0-9\-]{8,}")),
    ("inline db password", re.compile(r"(?i)\b\w+://[^\s:@]+:[^\s@]{3,}@")),
    ("home path", re.compile(r"/(?:home|Users)/[A-Za-z0-9._\-]+/")),
]


def find_leaks(text: str) -> list[tuple[str, str]]:
    """Anything credential-shaped surviving in the output. Empty list means publishable.

    Returns (label, redacted excerpt) so a failure can be located without printing the secret.
    """
    hits = []
    for label, rx in LEAK_CHECK:
        for m in rx.finditer(text or ""):
            s = m.group(0)
            # A placeholder must never be reported as a leak. Checking only the characters
            # BEFORE the match was not enough: an inline database URL matches starting at the
            # scheme, so the placeholder sits INSIDE the match and the check never saw it.
            if "[redacted:" in s:
                continue
            start = max(0, m.start() - 12)
            if "[redacted:" in (text[start:m.start()] or ""):
                continue
            hits.append((label, f"{s[:6]}… ({len(s)} chars) at offset {m.start()}"))
    return hits


def decode_base64_blobs(text: str, min_len: int = 40) -> list[str]:
    """Decoded base64 runs, so a secret hidden one encoding deep can still be checked.

    Not used to rewrite anything. It feeds `find_leaks` an extra view of the same document,
    because `echo $KEY | base64` is a completely ordinary thing to find in a transcript.
    """
    out = []
    for m in re.finditer(rf"[A-Za-z0-9+/]{{{min_len},}}={{0,2}}", text or ""):
        blob = m.group(0)
        try:
            dec = base64.b64decode(blob, validate=True).decode("utf-8", "ignore")
        except (binascii.Error, ValueError):
            continue
        if dec and sum(c.isprintable() for c in dec) / len(dec) > 0.8:
            out.append(dec)
    return out


def audit(text: str) -> list[tuple[str, str]]:
    """Every leak visible directly or one base64 layer down."""
    hits = find_leaks(text)
    for dec in decode_base64_blobs(text):
        hits.extend((f"{label} (base64-encoded)", ex) for label, ex in find_leaks(dec))
    return hits


DEFAULT_HOME = os.path.expanduser("~")
