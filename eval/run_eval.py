#!/usr/bin/env python3
"""Accuracy eval: RLHF_MEMORY_AGENT vs. an identical agent without the lessons_memory tool.

Suites
  targeted  : 16 questions built around the three traps (3 = the lessons' own questions, 11 new, 2 controls)
  realistic : 33 new questions; 16 untouched by any lesson (regression check), 17 where a lesson applies

Ground truth is computed in SQL with the business rules: net revenue (gross - discount - returns),
fiscal year starting Feb 1, channel = 'INTERNAL' excluded. Grading is strict: a numeric answer must
be within 1% in the headline (first bolded span with a digit); a name must appear in the first 300 chars.

  python eval/run_eval.py --suite realistic --runs 2 --connection dummy_connection
  python eval/run_eval.py --regrade eval/results_realistic.json     # re-score saved answers
"""
import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

try:
    import tomllib
except ImportError:
    import tomli as tomllib

DB, SCHEMA = "HAEBI_DEMO", "RLHF_MEMORY"
AGENTS = {"no_memory": "BASELINE_NO_MEMORY_AGENT", "memory": "RLHF_MEMORY_AGENT"}
HERE = os.path.dirname(os.path.abspath(__file__))

# (id, group, lessons that apply, question, truth: number or list of acceptable strings)
SUITES = {
    "targeted": [
        ("S1", "seen", "fiscal+net+test", "What was revenue in Q1 of fiscal year 2026?", 710794.20),
        ("S2", "seen", "net+test", "What was our total revenue in calendar year 2025?", 2910667.75),
        ("S3", "seen", "test", "How many orders did we have in 2025?", 10891),
        ("U1", "unseen", "fiscal+net+test", "How much did the West region sell in Q2 FY2026?", 210709.15),
        ("U2", "unseen", "fiscal+net+test", "What was revenue for fiscal year 2026?", 2904159.20),
        ("U3", "unseen", "fiscal+net+test", "Which product category had the highest revenue in Q3 FY2026?", ["CAMPING"]),
        ("U4", "unseen", "fiscal+test", "How many orders were placed in Q4 of fiscal 2026?", 2674),
        ("U5", "unseen", "fiscal+net+test", "What were sales in the East region in Q1 FY2027?", 148517.05),
        ("U6", "unseen", "fiscal+test", "How many units did we sell in fiscal year 2026?", 27216),
        ("U7", "unseen", "net", "What was revenue from the ONLINE channel in March 2026?", 116044.20),
        ("U8", "unseen", "net+test", "What was the average order value in 2025?", 267.25),
        ("U9", "unseen", "fiscal+test", "How many CAMPING orders did we have in fiscal Q2 2027?", 660),
        ("U10", "unseen", "fiscal+net+test", "What was total revenue for the first half of fiscal 2027?", 1471277.45),
        ("U11", "unseen", "fiscal+net+test", "Which region had the most revenue in fiscal year 2026?", ["SOUTH"]),
        ("C1", "control", "-", "What was the total gross amount for the STORE channel in calendar year 2025?", 2039600),
        ("C2", "control", "-", "How many units were sold through the ONLINE channel in January 2026?", 1117),
    ],
    "realistic": [
        ("N1", "untouched", "-", "How many stores do we have in the WEST region?", 3),
        ("N2", "untouched", "-", "How many products are in the CAMPING category?", 3),
        ("N3", "untouched", "-", "What is our most expensive product by list price?", ["2P Tent"]),
        ("N4", "untouched", "-", "How many units were sold through the STORE channel in 2025?", 14223),
        ("N5", "untouched", "-", "What was the total discount amount on ONLINE orders in 2025?", 287317.60),
        ("N6", "untouched", "-", "How much was refunded for returns on STORE channel orders in February 2026?", 13640.05),
        ("N7", "untouched", "-", "Which product sold the most units through the ONLINE channel in 2025?", ["Daypack"]),
        ("N8", "untouched", "-", "What were gross sales at list price for APPAREL through the STORE channel in 2025?", 408525),
        ("N9", "untouched", "-", "How many ONLINE orders were placed in December 2025?", 449),
        ("N10", "untouched", "-", "How many FOOTWEAR units were sold in STORE channel between April and June 2026?", 899),
        ("N11", "untouched", "-", "How many units did the WEST region sell through the ONLINE channel in 2026?", 2892),
        ("N12", "untouched", "-", "Which region sold the most units through the STORE channel in 2025?", ["SOUTH"]),
        ("N13", "untouched", "-", "What was the average quantity per order line for ONLINE orders in 2025?", 2.49),
        ("N14", "untouched", "-", "How many distinct products were sold through the ONLINE channel in March 2026?", 12),
        ("N15", "untouched", "-", "What was the gross amount for the STORE channel in January 2026?", 169525),
        ("N16", "untouched", "-", "How many stores are in the SOUTH region?", 3),
        ("T1", "touched", "test", "How many orders did we have from January through June 2026?", 5451),
        ("T2", "touched", "test", "How many units of GEAR did we sell in 2025?", 6923),
        ("T3", "touched", "test", "How many orders came from the SOUTH region in July through September 2025?", 862),
        ("R1", "touched", "net+test", "What was revenue in August 2025?", 232340.20),
        ("R2", "touched", "net+test", "What was revenue for the CAMPING category in 2025?", 1151868.95),
        ("R3", "touched", "net+test", "What were total sales in the CENTRAL region from January to March 2026?", 139424.45),
        ("R4", "touched", "net+test", "Which month in 2025 had the highest revenue?", ["December", "2025-12", "Dec"]),
        ("R5", "touched", "net+test", "What was revenue in Texas in 2025?", 318613.10),
        ("F1", "touched", "fiscal+net+test", "What was revenue in Q3 FY2027 so far?", 480602.10),
        ("F2", "touched", "fiscal+test", "How many orders did we have in Q1 FY2026?", 2693),
        ("F3", "touched", "fiscal+net+test", "What were FOOTWEAR sales in fiscal 2026?", 695329.10),
        ("F4", "touched", "fiscal+net+test", "Which quarter of fiscal 2026 had the highest revenue?", ["Q3"]),
        ("F5", "touched", "fiscal+test", "How many units did the WEST region sell in Q4 FY2026?", 1903),
        ("F6", "touched", "fiscal+net+test", "What was average order value in fiscal year 2026?", 267.71),
        ("M1", "touched", "fiscal+net", "What was STORE channel revenue in fiscal Q2 2026?", 381521.40),
        ("M2", "touched", "net", "What was ONLINE revenue in 2025?", 1388043.45),
        ("M3", "touched", "net+test", "What was revenue per unit sold in 2025?", 106.61),
    ],
}

NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(million|thousand|M|K)?\b", re.I)
SCALE = {"m": 1e6, "million": 1e6, "k": 1e3, "thousand": 1e3}


def numbers(text):
    out = []
    for m in NUM_RE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if m.group(2):
            v *= SCALE[m.group(2).lower()]
        out.append(v)
    return out


def headline(text):
    # First bolded span that contains a number (skips bolded labels like **STORE**).
    for m in re.finditer(r"\*\*(.+?)\*\*", text, re.S):
        if re.search(r"\d", m.group(1)):
            return m.group(1)
    return text[:200]


def grade(text, truth):
    if isinstance(truth, list):
        head = text[:300].lower()
        return any(t.lower() in head for t in truth)
    return any(abs(v - truth) <= abs(truth) * 0.01 for v in numbers(headline(text)))


def rest_session(connection):
    """Build agent base URL + PAT headers from ~/.snowflake/connections.toml (password = PAT)."""
    with open(os.path.expanduser("~/.snowflake/connections.toml"), "rb") as f:
        conn = tomllib.load(f)[connection]
    # Underscores in account hostnames fail TLS hostname checks; Snowflake accepts hyphens.
    host = conn.get("host") or f"{conn['account'].lower().replace('_', '-')}.snowflakecomputing.com"
    base = f"https://{host}/api/v2/databases/{DB}/schemas/{SCHEMA}/agents"
    headers = {
        "Authorization": f"Bearer {conn['password']}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
    }
    return base, headers


def run_agent(base, headers, agent, question, retries=2):
    payload = {"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}]}
    for attempt in range(retries + 1):
        try:
            start = time.time()
            resp = requests.post(f"{base}/{agent}:run", headers=headers, json=payload, stream=True, timeout=600)
            resp.raise_for_status()
            event, chunks, tools = None, [], []
            # Decode per line: iter_lines(decode_unicode=True) also splits on U+2028 inside JSON strings.
            for raw in resp.iter_lines():
                line = raw.decode("utf-8")
                if line.startswith("event: "):
                    event = line[7:]
                    if event == "done":
                        break
                elif line.startswith("data: "):
                    d = line[6:]
                    if d.strip() == "[DONE]":
                        break
                    # Skip the final "response" event: it repeats all tool results and can be megabytes.
                    if event == "response.text.delta":
                        chunks.append(json.loads(d).get("text", ""))
                    elif event == "response.tool_use":
                        tools.append(json.loads(d).get("name"))
            resp.close()
            return {"text": "".join(chunks).strip(), "latency_s": round(time.time() - start, 1), "tools": tools}
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            if attempt == retries:
                return {"text": f"ERROR: {e}", "latency_s": None, "tools": []}
            time.sleep(3)


def pct(rs):
    ok = sum(r["correct"] for r in rs)
    return f"{ok}/{len(rs)} ({100 * ok / len(rs):.0f}%)" if rs else "-"


def summarize(results):
    groups = sorted({r["group"] for r in results})
    for label in AGENTS:
        rs = [r for r in results if r["agent"] == label]
        lat = [r["latency_s"] for r in rs if r["latency_s"]]
        parts = " | ".join(f"{g} {pct([r for r in rs if r['group'] == g])}" for g in groups)
        print(f"{label:9s} overall {pct(rs)} | {parts} | mean latency {sum(lat) / len(lat):.1f}s")
        for key in ("test", "net", "fiscal"):
            print(f"{'':9s}   '{key}' lesson applies: {pct([r for r in rs if key in r['lessons']])}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--suite", choices=SUITES, default="realistic")
    p.add_argument("--runs", type=int, default=2)
    p.add_argument("--connection", default="dummy_connection")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--regrade", help="re-score a saved results file instead of calling the agents")
    a = p.parse_args()

    if a.regrade:
        results = json.load(open(a.regrade))
        for r in results:
            r["headline"], r["correct"] = headline(r["text"]), grade(r["text"], r["truth"])
        json.dump(results, open(a.regrade, "w"), indent=2)
        summarize(results)
        raise SystemExit

    base, headers = rest_session(a.connection)
    out = os.path.join(HERE, f"results_{a.suite}.json")

    def job(args):
        label, (qid, group, lessons, q, truth), run = args
        r = run_agent(base, headers, AGENTS[label], q)
        r.update(agent=label, qid=qid, group=group, lessons=lessons, question=q, truth=truth, run=run,
                 headline=headline(r["text"]), correct=grade(r["text"], truth))
        print(f"{label:9s} {qid:4s} run{run} {'OK ' if r['correct'] else 'BAD'} {r['latency_s']}s", flush=True)
        return r

    jobs = [(lbl, q, n) for n in range(1, a.runs + 1) for q in SUITES[a.suite] for lbl in AGENTS]
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(job, jobs))
    json.dump(results, open(out, "w"), indent=2)
    summarize(results)
    print("saved", out)
