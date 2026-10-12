#!/usr/bin/env python3
"""Minimal Cortex Agent REST client for the RLHF-memory demo.

  run      -> POST .../agents/{name}:run       (captures request_id, answer, SQL)
  feedback -> POST .../agents/{name}:feedback  (same API CoWork thumbs up/down uses)
"""
import argparse
import json
import sys

import requests
import snowflake.connector

DB, SCHEMA, AGENT = "HAEBI_DEMO", "RLHF_MEMORY", "RLHF_MEMORY_AGENT"


def _session(connection):
    conn = snowflake.connector.connect(connection_name=connection)
    # Underscores in account hostnames fail TLS hostname checks; Snowflake accepts hyphens.
    host = conn.host.replace("_", "-")
    base = f"https://{host}/api/v2/databases/{DB}/schemas/{SCHEMA}/agents/{AGENT}"
    headers = {
        "Authorization": f'Snowflake Token="{conn.rest.token}"',
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return base, headers


def run(connection, question):
    base, headers = _session(connection)
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}], "stream": False}
    r = requests.post(f"{base}:run", headers=headers, json=body, timeout=900)
    r.raise_for_status()
    request_id = r.headers.get("X-Snowflake-Request-Id") or r.headers.get("X-Snowflake-Request-ID")
    payload = r.json()

    answer, sql, tools_used, lessons = [], [], [], []
    for item in payload.get("content", []):
        t = item.get("type")
        if t == "text":
            answer.append(item.get("text", ""))
        elif t == "tool_use":
            tools_used.append(item.get("tool_use", {}).get("name"))
        elif t == "tool_result":
            tr = item.get("tool_result", {})
            for c in tr.get("content", []):
                j = c.get("json") or {}
                if j.get("sql"):
                    sql.append(j["sql"])
                for res in j.get("search_results", []) or []:
                    lessons.append(res.get("text") or res.get("doc_title"))
    return {
        "question": question,
        "request_id": request_id,
        "tools_used": tools_used,
        "lessons_retrieved": lessons,
        "sql": sql,
        "answer": "\n".join(answer).strip(),
    }


def feedback(connection, request_id, positive, message, categories):
    base, headers = _session(connection)
    body = {"orig_request_id": request_id, "positive": positive,
            "feedback_message": message, "categories": categories}
    r = requests.post(f"{base}:feedback", headers=headers, json=body, timeout=60)
    r.raise_for_status()
    return {"status_code": r.status_code, "body": r.text}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--connection", default="dummy_connection")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run")
    pr.add_argument("--question", action="append", required=True)
    pr.add_argument("--out")
    pf = sub.add_parser("feedback")
    pf.add_argument("--file", required=True, help="JSON list of {request_id, positive, message, categories}")
    a = p.parse_args()

    if a.cmd == "run":
        results = [run(a.connection, q) for q in a.question]
        out = json.dumps(results, indent=2)
        if a.out:
            open(a.out, "w").write(out)
        print(out)
    else:
        for f in json.load(open(a.file)):
            print(f["request_id"], feedback(a.connection, f["request_id"], f["positive"],
                                             f["message"], f.get("categories", [])))
