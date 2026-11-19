#!/usr/bin/env bash
# Verification for agent-replay.
#
# One property matters more than everything else here: the exported file must be safe to post
# publicly. So the checks are ordered by how badly they fail.
#
# Check 3 is the one that counts. It plants credentials of every recognised shape into a
# synthetic transcript, exports it, and then greps the produced HTML for the planted values
# with plain grep, not with this project's own checker. A redactor and a checker written by the
# same author can share a blind spot; a literal string search for the exact secret cannot.
#
# Check 4 is the other half. It exports a transcript containing no secrets and requires the
# output to be substantially unredacted, because a tool that redacts everything passes check 3
# perfectly and is useless.
#
# Attacked on 2026-07-26 by disabling each redaction mechanism in turn, and by making the
# post-render audit always return clean. Checks 3 and 5 caught them. See README.
set -euo pipefail
cd "$(dirname "$0")/.."
mode=${1:-}
if [ "$#" -gt 1 ] || { [ -n "$mode" ] && [ "$mode" != "--offline" ]; }; then
  echo "usage: bash scripts/verify.sh [--offline]" >&2
  exit 2
fi

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "1. unit suite"
if out=$(python3 -m unittest discover -s tests -t . 2>&1); then
  ok "$(printf '%s' "$out" | grep -oE 'Ran [0-9]+ tests') passed"
else
  printf '%s\n' "$out" | tail -25
  bad "unit suite"
fi

echo
echo "2. a synthetic transcript exports end to end"
python3 scripts/make_fixture.py "$work/clean.jsonl" --clean
python3 scripts/make_fixture.py "$work/dirty.jsonl" --with-secrets
if python3 -m agent_replay.cli "$work/dirty.jsonl" -o "$work/out.html" >/dev/null 2>"$work/e"; then
  size=$(wc -c < "$work/out.html")
  ok "exported a $size byte replay"
else
  cat "$work/e"; bad "the export failed"
fi

echo
echo "3. every planted secret is absent from the exported file"
# The check that matters. grep for the literal planted values, using no code from this project.
if [ -f "$work/out.html" ]; then
  planted=$(python3 scripts/make_fixture.py --print-secrets)
  leaked=0
  total=0
  while IFS= read -r secret; do
    [ -z "$secret" ] && continue
    total=$((total + 1))
    if grep -qF -- "$secret" "$work/out.html"; then
      leaked=$((leaked + 1))
      printf '    LEAKED: %s… (%s chars)\n' "${secret:0:8}" "${#secret}"
    fi
  done <<< "$planted"
  if [ "$total" -lt 10 ]; then
    bad "only $total secrets were planted, too few for this check to mean anything"
  elif [ "$leaked" -ne 0 ]; then
    bad "$leaked of $total planted secrets survived into the exported HTML"
  else
    ok "all $total planted secrets are absent, checked with grep -F against the literal values"
  fi
else
  bad "no export to check"
fi

echo
echo "3b. each redaction mechanism is individually alive"
# Found by attacking this script: disabling the known-format pass entirely, or the
# assignment-context pass entirely, still passed every check above, because the three layers
# cover for one another. That redundancy is exactly what you want at runtime and it is useless
# for verification, since a dead layer is invisible. So each mechanism is probed with a secret
# that ONLY it can catch, and a layer that stops working now fails the build.
if python3 - <<'PYCHK'
import sys
from agent_replay.redact import redact

# Only a known format can catch this: no secret-ish name nearby, low enough entropy in context,
# and nothing that looks like an assignment.
ONLY_FORMAT = "please use ghp_" + "aB3dE5gH7jK9mN1pQ3rS5tU7wX9yZ1" + " when you push"
# Only assignment context can catch this: a real word as the value, matching no key format and
# far too low-entropy for the entropy pass.
ONLY_ASSIGNMENT = "ADMIN_PASSWORD=correcthorsebattery"
# Only the entropy pass can catch this: sits near "api_key" as prose, not an assignment, and
# matches no known prefix.
ONLY_ENTROPY = "the api_key we were issued reads zQ8vN2mK9pL4wR7tY1uI3oA6sD5fG0hJx7 exactly"

fails = []
for label, probe in (("known-format", ONLY_FORMAT),
                     ("assignment-context", ONLY_ASSIGNMENT),
                     ("entropy", ONLY_ENTROPY)):
    r = redact(probe)
    print(f"  {label:<20} {'alive' if r.total else 'DEAD'}   -> {r.text[:64]}")
    if not r.total:
        fails.append(label)
if fails:
    print(f"    these mechanisms caught nothing: {fails}")
    sys.exit(1)
PYCHK
then
  ok "all three redaction mechanisms independently catch something"
else
  bad "a redaction mechanism is dead, and the other layers were hiding it"
fi

echo
echo "4. a transcript with no secrets is NOT mangled"
# A redactor that blanks everything passes check 3 and is worthless.
if python3 -m agent_replay.cli "$work/clean.jsonl" -o "$work/clean.html" >/dev/null 2>&1; then
  if python3 - "$work/clean.html" <<'PY'
import sys, re
h = open(sys.argv[1], encoding="utf-8").read()
n = len(re.findall(r"\[redacted:", h))
markers = [m for m in re.findall(r"\[redacted:([a-z\-]+)\]", h) if m != "home-path"]
print(f"  {n} redaction marker(s) in a transcript with no secrets in it")
if markers:
    print(f"    unexpected: {sorted(set(markers))}")
    sys.exit(1)
# The real content must still be there.
for phrase in ("the test suite passed", "src/components/Widget.tsx", "npm install"):
    if phrase not in h:
        print(f"    ordinary content went missing: {phrase!r}")
        sys.exit(1)
PY
  then
    ok "clean content survives intact, so the tool is not simply blanking everything"
  else
    bad "a clean transcript was over-redacted"
  fi
else
  bad "the clean export failed"
fi

echo
echo "5. the exporter REFUSES when something survives"
# The refusal is the safety net. If it can be made not to fire, the net is decorative.
if python3 - "$work" <<'PY'
import subprocess, sys, os, json
work = sys.argv[1]
# A transcript whose secret is a shape the redactor does not know, but the checker does.
rec = {"type": "user", "uuid": "u1", "timestamp": "2026-07-26T00:00:00Z",
       "message": {"role": "user", "content": [
           {"type": "text", "text": "value: " + "AKIA" + "QWERTYUIOPASDFGH"}]}}
path = os.path.join(work, "refuse.jsonl")
open(path, "w").write(json.dumps(rec) + "\n")
# Neutralise only the KNOWN-format pass, so the checker still sees the AWS key.
import agent_replay.redact as R
R.KNOWN = [k for k in R.KNOWN if k[0] != "aws-access-key"]
from agent_replay import cli
rc = cli.main([path, "-o", os.path.join(work, "refused.html")])
wrote = os.path.exists(os.path.join(work, "refused.html"))
print(f"  exit={rc} file_written={wrote}")
sys.exit(0 if rc != 0 and not wrote else 1)
PY
then
  ok "a surviving credential blocks the write and exits nonzero"
else
  bad "the exporter wrote a file containing a credential the checker had flagged"
fi

echo
echo "6. the export is genuinely self-contained"
if [ -f "$work/out.html" ]; then
  if python3 - "$work/out.html" <<'PY'
import sys, re
h = open(sys.argv[1], encoding="utf-8").read()
bad = []
if re.search(r'<(?:script|link)[^>]+(?:src|href)="https?://', h): bad.append("remote script or stylesheet")
if re.search(r'@import\s+url\(["\']?https?://', h): bad.append("remote @import")
if re.search(r'<img[^>]+src="https?://', h): bad.append("remote image")
if not h.lstrip().lower().startswith("<!doctype"): bad.append("no doctype")
if 'name="viewport"' not in h: bad.append("no viewport meta")
if "prefers-color-scheme" not in h: bad.append("no dark mode")
if 'data-theme=dark' not in h and 'data-theme="dark"' not in h: bad.append("no theme override")
if re.search(r"/(?:home|Users)/[A-Za-z0-9._-]+/", h): bad.append("absolute home path")
print("  " + (", ".join(bad) if bad else "no remote assets, doctype and viewport present, both themes"))
sys.exit(1 if bad else 0)
PY
  then ok "self-contained, standards mode, both themes, no personal paths"
  else bad "the exported page is not safely self-contained"; fi
fi

echo
echo "7. the cost model keeps pricing identity and confidence explicit"
# The easiest way to publish a wrong number, and it is off by several times on a real run.
if python3 - <<'PY'
from agent_replay.cost import price, rate_for
usage = {"input_tokens": 1000, "output_tokens": 1000,
         "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 0}
correct, conf = price(usage, "claude-sonnet-4-5")
naive = (1000 + 1_000_000) * 3.0 / 1e6 + 1000 * 15.0 / 1e6
print(f"  cache-aware ${correct:.4f}  naive ${naive:.4f}  ratio {naive/correct:.1f}x")
import sys
if correct >= naive * 0.5:
    print("    cached tokens are not being discounted"); sys.exit(1)
if conf != "exact":
    print("    a known model was not priced exactly"); sys.exit(1)
_, c2 = rate_for("claude-opus-5")
if c2 != "inferred":
    print(f"    an unlisted model should be reported as inferred, got {c2}"); sys.exit(1)
_, c3 = rate_for("claude-opus-4-999")
if c3 == "exact":
    print("    a lookalike model id was incorrectly priced as exact"); sys.exit(1)
unknown, c4 = price(usage, "provider-model-with-no-rate")
if unknown is not None or c4 != "unknown":
    print(f"    an unknown model should be unpriced, got {unknown!r} ({c4})"); sys.exit(1)
PY
then ok "cache reads are discounted, aliases are explicit, and unknown rates stay unavailable"
else bad "the cost model is wrong"; fi

echo
echo "8. a real transcript, if one is present"
if [ "$mode" = "--offline" ]; then
  echo "  skipped: offline run uses synthetic fixtures only"
else
  REAL=$(ls -S "$HOME"/.claude/projects/*/*.jsonl 2>/dev/null | head -1 || true)
  if [ -n "$REAL" ] && [ -f "$REAL" ]; then
    if python3 -m agent_replay.cli "$REAL" -o "$work/real.html" 2>"$work/re" >/dev/null; then
      n=$(grep -c 'class="ev' "$work/real.html" 2>/dev/null || echo 0)
      ok "exported a real transcript ($(wc -c < "$work/real.html" | tr -d ' ') bytes)"
    else
      cat "$work/re" | head -5
      bad "a real transcript failed to export, which is the actual use case"
    fi
  else
    bad "no real transcript available, so the end-to-end path was not exercised"
  fi
fi

echo
echo "9. no secrets in this repository's own tracked files"
# Two separate questions, because they have different answers.
#
# First: is THIS machine's home path committed? That is the privacy failure, and it is exact.
#
# Second: is anything credential-shaped committed? AWS publishes AKIAIOSFODNN7EXAMPLE in its own
# documentation for exactly this purpose, so it is allowed.
#
# A generic /home/<name>/ pattern is deliberately NOT used. Tests and comments here have to
# contain fictional paths in order to test path handling at all, and flagging /home/someone/ in a
# test fixture is a false alarm that trains people to ignore this check.
personal=0
if grep -rIFn -- "$HOME" --include='*.py' --include='*.md' --include='*.sh' --include='*.toml' \
      agent_replay tests scripts README.md 2>/dev/null; then
  personal=1
fi
creds=$(grep -rIEn 'sk-ant-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|AIza[A-Za-z0-9_-]{35}' \
      --include='*.py' --include='*.md' --include='*.sh' --include='*.toml' \
      agent_replay tests scripts README.md 2>/dev/null | grep -v 'AKIAIOSFODNN7EXAMPLE' || true)
if [ "$personal" -ne 0 ]; then
  bad "this machine's home path is committed"
elif [ -n "$creds" ]; then
  printf '%s\n' "$creds" | head -6
  bad "found credential-shaped strings"
else
  ok "no real home path and no credential-shaped strings (AWS's documented example key allowed)"
fi

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
if [ "$fail" -ne 0 ]; then echo "VERIFY FAILED"; exit 1; fi
if [ "$mode" = "--offline" ]; then echo "OFFLINE VERIFY OK"; else echo "VERIFY OK"; fi
