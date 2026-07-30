#!/usr/bin/env python3
"""Build synthetic transcripts for the verify script.

The secrets here are assembled at runtime from character tables rather than written as
literals. GitHub's push protection scans full history and rejects a synthetic-but-plausible
credential, which blocks the entire push, so a fixture file containing complete key-shaped
strings would make this repository unpushable. Assembling them at runtime means no complete
pattern exists on disk while the test still exercises the real shapes.

`--print-secrets` emits the exact planted values so verify can grep the exported HTML for them
with plain `grep -F`, using none of this project's own matching code. A redactor and a checker
written by the same author can share a blind spot. A literal search for the exact secret cannot.
"""
from __future__ import annotations

import argparse
import json
import sys

A = "abcdefghijklmnopqrstuvwxyz"
D = "0123456789"


def body(n: int, salt: int = 0) -> str:
    out = []
    for i in range(n):
        j = (i * 7 + salt * 13) % 26
        out.append(A[j].upper() if i % 3 == 0 else (D[(i + salt) % 10] if i % 3 == 1 else A[j]))
    return "".join(out)


def secrets() -> dict:
    """Every shape the redactor claims to handle, one instance each."""
    return {
        "anthropic":  "sk-ant-api03-" + body(60, 1),
        "openai":     "sk-proj-" + body(44, 2),
        "openrouter": "sk-or-v1-" + body(64, 3),
        "github":     "ghp_" + body(36, 4),
        "github_pat": "github_pat_" + body(60, 5),
        "gitlab":     "glpat-" + body(20, 6),
        "google":     "AIza" + body(35, 7),
        "aws":        "AKIA" + "".join(c.upper() for c in body(16, 8) if c.isalnum())[:16],
        "slack":      "xoxb-" + body(28, 9),
        "stripe":     "sk_live_" + body(26, 10),
        "hf":         "hf_" + body(34, 11),
        "npm":        "npm_" + body(36, 12),
        "password":   "P4ssw0rd" + body(14, 13),
        "dbpass":     "Db" + body(12, 14),
        "homegrown":  body(32, 15),
    }


def records(with_secrets: bool):
    s = secrets()
    ts = "2026-07-26T21:{:02d}:{:02d}.000Z"
    out = []

    def rec(i, rtype, content, **kw):
        r = {"type": rtype, "uuid": f"u{i}", "parentUuid": f"u{i-1}" if i else None,
             "timestamp": ts.format(i // 60, i % 60), "sessionId": "fixture",
             "isSidechain": False, "cwd": "/tmp/fixture",
             "message": {"role": "user" if rtype == "user" else "assistant",
                         "content": content}}
        if rtype == "assistant":
            r["message"]["model"] = kw.get("model", "claude-sonnet-4-5")
            r["message"]["usage"] = kw.get("usage", {
                "input_tokens": 120, "output_tokens": 340,
                "cache_read_input_tokens": 48000, "cache_creation_input_tokens": 1200,
                "cache_creation": {"ephemeral_5m_input_tokens": 1200,
                                   "ephemeral_1h_input_tokens": 0}})
        return r

    i = 0
    out.append(rec(i, "user", [{"type": "text", "text":
        "Please set up deployment and run the test suite."}])); i += 1
    out.append(rec(i, "assistant", [{"type": "text", "text":
        "I will configure the environment and then run the test suite."}])); i += 1

    env = "\n".join([
        "NODE_ENV=production",
        "PORT=3000",
        "DEBUG_TOKEN_PARSER=true",
    ])
    if with_secrets:
        env = "\n".join([
            env,
            f"ANTHROPIC_API_KEY={s['anthropic']}",
            f"OPENAI_API_KEY={s['openai']}",
            f"OPENROUTER_API_KEY={s['openrouter']}",
            f"GITHUB_TOKEN={s['github']}",
            f"GITHUB_PAT={s['github_pat']}",
            f"GITLAB_TOKEN={s['gitlab']}",
            f"GOOGLE_API_KEY={s['google']}",
            f"AWS_ACCESS_KEY_ID={s['aws']}",
            f"SLACK_BOT_TOKEN={s['slack']}",
            f"STRIPE_SECRET_KEY={s['stripe']}",
            f"HF_TOKEN={s['hf']}",
            f"NPM_TOKEN={s['npm']}",
            f"ADMIN_PASSWORD={s['password']}",
            f"MY_COMPANY_SECRET={s['homegrown']}",
            f"DATABASE_URL=postgres://app:{s['dbpass']}@db.internal:5432/prod",
        ])
    out.append(rec(i, "assistant", [{"type": "tool_use", "id": "t1", "name": "Read",
                                     "input": {"file_path": ".env"}}])); i += 1
    out.append(rec(i, "user", [{"type": "tool_result", "tool_use_id": "t1",
                                "content": env}])); i += 1

    out.append(rec(i, "assistant", [{"type": "tool_use", "id": "t2", "name": "Edit",
        "input": {"file_path": "src/components/Widget.tsx",
                  "old_string": "const timeout = 500;",
                  "new_string": "const timeout = 2000;"}}])); i += 1
    out.append(rec(i, "user", [{"type": "tool_result", "tool_use_id": "t2",
                                "content": "The file has been updated."}])); i += 1

    out.append(rec(i, "assistant", [{"type": "tool_use", "id": "t3", "name": "Bash",
        "input": {"command": "npm install && npm test",
                  "description": "Install dependencies and run tests"}}])); i += 1
    out.append(rec(i, "user", [{"type": "tool_result", "tool_use_id": "t3", "content":
        "npm install completed\n27 passing\nthe test suite passed in 4.2s"}])); i += 1

    out.append(rec(i, "assistant", [{"type": "tool_use", "id": "t4", "name": "Bash",
        "input": {"command": "curl -H 'Authorization: Bearer "
                             + (s["github"] if with_secrets else "$TOKEN")
                             + "' https://api.example.com/deploy"}}])); i += 1
    out.append(rec(i, "user", [{"type": "tool_result", "tool_use_id": "t4",
                                "content": "deploy accepted", "is_error": False}])); i += 1

    out.append(rec(i, "user", [{"type": "tool_result", "tool_use_id": "t5",
                                "content": "command not found: deploy-tool",
                                "is_error": True}])); i += 1
    out.append(rec(i, "assistant", [{"type": "text", "text":
        "The test suite passed and the change to src/components/Widget.tsx is in place. "
        "Commit 6f4e5647a2b1c3d4e5f6a7b8c9d0e1f2a3b4c5d6 records it."}],
        model="claude-opus-5")); i += 1
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("out", nargs="?")
    p.add_argument("--with-secrets", action="store_true")
    p.add_argument("--clean", action="store_true")
    p.add_argument("--print-secrets", action="store_true")
    a = p.parse_args(argv)

    if a.print_secrets:
        for v in secrets().values():
            print(v)
        return 0
    if not a.out:
        sys.stderr.write("give an output path, or --print-secrets\n")
        return 2
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in records(with_secrets=a.with_secrets):
            fh.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
