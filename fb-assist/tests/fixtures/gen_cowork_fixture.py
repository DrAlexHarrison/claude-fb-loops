"""Generate the synthetic Cowork audit.jsonl fixture. ALL secrets/PII are FAKE.

Models the PINNED Cowork audit.jsonl shape (snake_case envelope):
  {_audit_timestamp, message:{content, role}, parent_tool_use_id, session_id, type, uuid}
message.content is a string OR a list of CC-style blocks
(text / thinking / tool_use / tool_result). NO top-level toolUseResult mirror —
tool output lives ONLY inside the message.content tool_result block.
"""
import json
from pathlib import Path

SID = "local_ditto_0a1b2c3d4e5f6071"

# ---- FAKE sentinels (planted; every one must be byte-absent post-redaction) ----
SECRET_KEY = "sk-ant-api03-FAKEcowork0000aaaa1111bbbb2222cccc3333dddd4444EE"
EMAIL = "dana.cowork@northwind-labs.example"
SSN = "321-54-9876"
AWS_KEY = "AKIAFAKECOWORK1234XY"
DB_PASS = "hunter2-cowork-FAKE-pw"
IP = "10.4.5.6"
CODENAME = "Project Nimbus"
PATH = "/Users/dana/Claude/nimbus/secrets.env"

TS = "2026-06-30T18:0{}:00.000Z"


def rec(i, role, content, uuid, parent=None, rtype=None):
    return {
        "_audit_timestamp": TS.format(i),
        "session_id": SID,
        "parent_tool_use_id": parent,
        "type": rtype or role,
        "uuid": uuid,
        "message": {"role": role, "content": content},
    }


records = []

# 0) human prompt (string content) — narrative carrying secret+email+codename+path+ssn
records.append(rec(
    0, "user",
    f"The Cowork agent froze mid-task on {CODENAME}, our internal billing rewrite. "
    f"I'm authed with {SECRET_KEY} and you can reach me at {EMAIL}. "
    f"My SSN on file is {SSN}. The config it choked on is {PATH}.",
    "u0",
))

# 1) assistant — thinking + text + tool_use(Read, file_path=PATH)
records.append(rec(
    1, "assistant",
    [
        {"type": "thinking", "thinking": f"User mentioned {CODENAME} and a path {PATH}. "
                                          f"Their key {SECRET_KEY} should never be echoed.",
         "signature": "sigFAKE=="},
        {"type": "text", "text": "Let me read that config to see what froze."},
        {"type": "tool_use", "id": "toolu_read_1", "name": "Read",
         "input": {"file_path": PATH}},
    ],
    "a1",
))

# 2) tool_result for the Read — file_contents (env file with AWS key + db pass)
records.append(rec(
    2, "user",
    [
        {"type": "tool_result", "tool_use_id": "toolu_read_1",
         "content": [
             {"type": "text",
              "text": f"AWS_ACCESS_KEY_ID={AWS_KEY}\nDB_PASSWORD={DB_PASS}\n"
                      f"# owner {EMAIL}\nSERVICE=nimbus-billing\n"}
         ]}
    ],
    "u2", parent="toolu_read_1",
))

# 3) assistant — text + tool_use(Bash, command carrying a secret)
records.append(rec(
    3, "assistant",
    [
        {"type": "text", "text": "I'll check the running service env."},
        {"type": "tool_use", "id": "toolu_bash_1", "name": "Bash",
         "input": {"command": f"DB_PASSWORD='{DB_PASS}' curl -H 'x-api-key: {SECRET_KEY}' "
                              f"https://api.internal/health"}},
    ],
    "a3",
))

# 4) tool_result for the Bash — bash_output (stdout with an internal IP)
records.append(rec(
    4, "user",
    [
        {"type": "tool_result", "tool_use_id": "toolu_bash_1",
         "content": f"connected to {IP}:8443\nbilling-svc OK\n", "is_error": False}
    ],
    "u4", parent="toolu_bash_1",
))

# 5) assistant — text + tool_use(WebSearch, query)
records.append(rec(
    5, "assistant",
    [
        {"type": "text", "text": "Searching for the freeze signature."},
        {"type": "tool_use", "id": "toolu_web_1", "name": "WebSearch",
         "input": {"query": f"{CODENAME} cowork agent submit freeze"}},
    ],
    "a5",
))

# 6) tool_result for the WebSearch — websearch results
records.append(rec(
    6, "user",
    [
        {"type": "tool_result", "tool_use_id": "toolu_web_1",
         "content": [
             {"type": "text",
              "text": f"Result: Nimbus billing internal runbook ({EMAIL}) — "
                      f"connect via {IP}; key {SECRET_KEY}."}
         ]}
    ],
    "u6", parent="toolu_web_1",
))

# 7) assistant — final narrative (assistant_text) re-mentioning the path
records.append(rec(
    7, "assistant",
    [
        {"type": "text",
         "text": f"The freeze happens because {PATH} is read on the UI thread. "
                 f"I'd move the {CODENAME} config load off-thread."}
    ],
    "a7",
))

# 8) human prompt (string content again) — exercises the str branch + a trailing secret
records.append(rec(
    8, "user",
    f"Thanks. Don't include my key {SECRET_KEY} in any feedback you send Anthropic.",
    "u8",
))

# 9) assistant — string content (exercises assistant str branch)
records.append(rec(
    9, "assistant",
    "Understood — I'll scrub credentials before anything leaves your machine.",
    "a9",
))

out = Path(__file__).resolve().parent / "cowork-audit.jsonl"
with open(out, "w", encoding="utf-8") as fh:
    for r in records:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"wrote {out} ({len(records)} records)")
