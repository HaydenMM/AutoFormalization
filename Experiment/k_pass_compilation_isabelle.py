#!/usr/bin/env python3
import os, re, json, argparse, textwrap, concurrent.futures
from typing import List, Dict, Any
from isabelle_client import get_isabelle_client

# ---------------- Timeout helper (unchanged behavior) ----------------
def run_with_timeout(fn, timeout_s: float, /, *args, **kwargs):
    """
    Run a blocking function with a hard wall-clock timeout.
    IMPORTANT: Avoids waiting for stuck worker threads by calling
    shutdown(wait=False, cancel_futures=True) on timeout.
    """
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="isa-call")
    fut = ex.submit(lambda: fn(*args, **kwargs))
    timed_out = False
    try:
        return fut.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        timed_out = True
        fut.cancel()
        ex.shutdown(wait=False, cancel_futures=True)
        return {
            "ok": False,
            "errors": [{"message": {"kind": "ERROR",
                                    "message": f"theory check timed out after {timeout_s:.1f}s"}}],
            "timed_out": True,
        }
    finally:
        if not timed_out:
            try:
                ex.shutdown(wait=True, cancel_futures=True)
            except Exception:
                pass

# ---------------- Helpers for client return-shape quirks ----------------
def get_session_id(x):
    return x.get("session_id") if isinstance(x, dict) else x

def coerce_use_theories_result(res):
    if isinstance(res, dict):
        return res
    if isinstance(res, list) and res:
        last = res[-1]
        body = getattr(last, "response_body", None)
        if body is None and isinstance(last, dict):
            body = last.get("response_body")
        if isinstance(body, str):
            try:
                return json.loads(body)
            except Exception:
                pass
        for fr in reversed(res):
            b = getattr(fr, "response_body", None)
            if b is None and isinstance(fr, dict):
                b = fr.get("response_body")
            if isinstance(b, str):
                try:
                    return json.loads(b)
                except Exception:
                    continue
        frames = []
        for fr in res:
            frames.append({
                "type": getattr(fr, "response_type", getattr(fr, "type", None)),
                "body": getattr(fr, "response_body", getattr(fr, "body", None)),
            })
        return {"ok": False, "frames": frames}
    return {"ok": False, "errors": [{"message": {"kind": "ERROR", "message": f"Unexpected response type: {type(res)}"}}]}

# ---------------- Name + theory wrappers (unchanged) ----------------
def sanitize_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not s:
        s = "T"
    if not s[0].isalpha():
        s = "T_" + s
    s = s[0].upper() + s[1:]
    return s[:120]

# ---------------- GPT snippet normalization (unchanged) ----------------
def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            body = s[nl+1:]
            end = body.rfind("```")
            if end != -1:
                return body[:end].strip()
    return s

def _strip_wrapping_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in {'"', "'"}:
        return s[1:-1].strip()
    return s

def _drop_leading_theorem(snippet: str) -> str:
    s = _strip_wrapping_quotes(_strip_code_fences(snippet))
    lines = s.splitlines()
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines):
        return ""
    first = lines[i].strip()
    low = first.lower()
    if not low.startswith("theorem"):
        return s.strip()
    rest = first[len("theorem"):].lstrip()
    if rest.startswith(":"):
        rest = rest[1:].lstrip()
    if rest:
        tail = "\n".join(lines[i+1:]).strip()
        return (rest + ("\n" + tail if tail else "")).strip()
    else:
        return "\n".join(lines[i+1:]).strip()

def normalize_lets(term: str) -> str:
    parts = [ln.strip() for ln in term.strip().splitlines() if ln.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    bindings, body_parts, seen_in = [], [], False

    def clean_bind(b: str) -> str:
        b = b.strip()
        if b.lower().startswith("let "):
            b = b[4:].lstrip()
        if b.endswith(";"):
            b = b[:-1].rstrip()
        return b

    for ln in parts:
        low = ln.lower()
        if not seen_in:
            if low.startswith("in "):
                seen_in = True
                body_parts.append(ln[3:].lstrip())
            elif low.startswith("let "):
                rest = ln[4:].lstrip()
                for chunk in [c.strip() for c in rest.split(";") if c.strip()]:
                    bindings.append(chunk)
            else:
                seen_in = True
                body_parts.append(ln)
        else:
            body_parts.append(ln)

    body = " ".join(body_parts).strip()
    binds = [clean_bind(b) for b in bindings if b]
    return f"let {'; '.join(binds)} in {body}" if (binds and body) else term.strip()

def make_isar_from_gpt_formal(gpt_text: str) -> str:
    after = _drop_leading_theorem(gpt_text)
    head = after.lstrip()
    head_low = head.lower()
    for kw in ("lemma ", "theorem ", "proposition ", "corollary ", "fact "):
        if head_low.startswith(kw):
            return f"{after.rstrip()}\nsorry\n"
    if head.startswith('shows "'):
        return "theorem\n  " + head.rstrip() + "\n" + "sorry\n"
    term = normalize_lets(after)
    term_escaped = term.replace('"', '\\"')
    return "theorem\n  shows \"" + term_escaped + "\"\n" + "sorry\n"

def wrap_as_theory(theory_name: str, gpt_text: str) -> str:
    head = gpt_text.lstrip()
    if head.lower().startswith("theory ") and " begin" in head:
        return gpt_text if gpt_text.strip().endswith("end") else (gpt_text + "\nend\n")
    isar = make_isar_from_gpt_formal(gpt_text).strip()
    return textwrap.dedent(f"""\
        theory {theory_name}
        imports Main
        begin

        {isar}

        end
    """)

# ---------------- Pretty-print errors (unchanged behavior) ----------------
def _fmt_pos(pos) -> str:
    if not isinstance(pos, dict):
        return ""
    return f"  at {pos.get('file','<unknown>')}:{pos.get('line','?')}:{pos.get('offset','?')}"

def print_isabelle_errors(res_dict, prefix=""):
    errs = res_dict.get("errors", [])
    if isinstance(errs, (str, bytes)):
        print(f"{prefix}[ERROR] {errs}")
        return
    if isinstance(errs, dict):
        errs = [errs]
    if not isinstance(errs, list):
        print(f"{prefix}[ERROR] (unexpected errors payload: {type(errs)}) {errs}")
        return
    printed_any = False
    for e in errs:
        if isinstance(e, (str, bytes)):
            print(f"{prefix}[ERROR] {e}"); printed_any = True; continue
        if not isinstance(e, dict):
            print(f"{prefix}[ERROR] (unexpected error entry: {type(e)}) {e}"); printed_any = True; continue
        msg = e.get("message")
        if isinstance(msg, dict):
            kind = msg.get("kind", "ERROR")
            mtxt = msg.get("message", "")
            pos  = msg.get("pos", {})
            print(f"{prefix}[{kind}] {mtxt}")
            p = _fmt_pos(pos)
            if p: print(prefix + p)
            printed_any = True
        elif isinstance(msg, (str, bytes)):
            print(f"{prefix}[ERROR] {msg}"); printed_any = True
        else:
            print(f"{prefix}[ERROR] {e}"); printed_any = True
    if not printed_any:
        print(f"{prefix}(no 'errors' entries to print)")

def dump_nodes(res, prefix="   "):
    printed = False
    nodes = res.get("nodes") or []
    if isinstance(nodes, dict):
        nodes = [nodes]
    for n in nodes:
        node_name = (n.get("name") if isinstance(n, dict) else None) or ""
        messages = []
        if isinstance(n, dict):
            messages = n.get("messages") or []
        if isinstance(messages, dict):
            messages = [messages]
        for m in messages:
            mm   = (m.get("message") if isinstance(m, dict) else None) or {}
            kind = (mm.get("kind") if isinstance(mm, dict) else None) or "INFO"
            text = (mm.get("message") if isinstance(mm, dict) else None)
            if text is None and isinstance(m, dict) and isinstance(m.get("message"), str):
                text = m["message"]
            if text is None:
                text = str(m)
            pos  = (mm.get("pos") if isinstance(mm, dict) else None) or {}
            loc  = _fmt_pos(pos)
            header = f"{prefix}[{kind}] "
            if node_name:
                header += f"({node_name}) "
            print(header + str(text).strip())
            if loc:
                print(prefix + loc)
            printed = True
    if not printed and "frames" in res:
        for fr in (res.get("frames") or []):
            ftype = fr.get("type")
            body  = fr.get("body")
            if isinstance(body, str):
                body = body.strip()
            body_preview = (str(body)[:400] + ("…" if body and len(str(body)) > 400 else ""))
            print(f"{prefix}[FRAME {ftype}] {body_preview}")
            printed = True
    if not printed:
        print(f"{prefix}(no node messages)")

# ---------------- Pass@K runner ----------------
def first_k_candidates(obj: Dict[str, Any], key_list: str, key_single: str, k: int) -> List[str]:
    cands = []
    if key_list in obj and isinstance(obj[key_list], list):
        cands = [s for s in obj[key_list] if isinstance(s, str)]
    elif key_single in obj and isinstance(obj[key_single], str):
        cands = [obj[key_single]]
    return cands[:k]

def compile_one_theory(isa, sid, work_dir_abs, theory_name: str, snippet: str, timeout_s: float):
    # write theory
    thy_path = os.path.join(work_dir_abs, f"{theory_name}.thy")
    with open(thy_path, "w", encoding="utf-8") as tf:
        tf.write(wrap_as_theory(theory_name, snippet))
    # compile with timeout
    res_raw = run_with_timeout(
        isa.use_theories, timeout_s,
        session_id=sid,
        theories=[theory_name],
        master_dir=work_dir_abs,
        unicode_symbols=True,
        watchdog_timeout=int(timeout_s)
    )
    return coerce_use_theories_result(res_raw)

def main():
    ap = argparse.ArgumentParser(description="Pass@K for GPT-formal statements via Isabelle Server")
    ap.add_argument("--server-line", required=True)
    ap.add_argument("--json", required=True)
    ap.add_argument("--key-field", default="gpt_formal_s")              # fallback single
    ap.add_argument("--key-field-list", default="gpt_formal_s_list")    # primary list
    ap.add_argument("--session", default="HOL")
    ap.add_argument("--work-dir", default="isabelle_batch_tmp")
    ap.add_argument("--log", default="passk_log.jsonl")
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--ks", default="1,2,3,5,10", help="Comma-separated K values")
    args = ap.parse_args()

    ks: List[int] = sorted({int(x) for x in args.ks.split(",") if x.strip().isdigit()})
    work_dir_abs = os.path.abspath(args.work_dir)
    os.makedirs(work_dir_abs, exist_ok=True)
    data = json.load(open(args.json, "r", encoding="utf-8"))

    isa = get_isabelle_client(args.server_line)
    print("✅ Connected to Isabelle server")

    # Build + session
    isa.session_build(session=args.session, dirs=[work_dir_abs])
    sid = get_session_id(isa.session_start(session=args.session))
    print("🧠 Session started:", sid)

    # Optional probe
    if args.probe:
        probe = "Probe"
        with open(os.path.join(work_dir_abs, f"{probe}.thy"), "w", encoding="utf-8") as f:
            f.write(f"""theory {probe}
imports Main
begin
lemma "True" by simp
end
""")
        res = coerce_use_theories_result(run_with_timeout(
            isa.use_theories, args.timeout,
            session_id=sid, theories=[probe], master_dir=work_dir_abs,
            unicode_symbols=True, watchdog_timeout=int(args.timeout)
        ))
        print("🔎 Probe OK:", res.get("ok"))
        if not res.get("ok"):
            print_isabelle_errors(res, prefix="   ")
            dump_nodes(res, prefix="   ")

    items = list(data.items())
    if args.limit > 0:
        items = items[:args.limit]

    totals = 0
    pass_at = {k: 0 for k in ks}

    with open(args.log, "a", encoding="utf-8") as logf:
        for idx, (qid, obj) in enumerate(items, 1):
            totals += 1
            base_name = sanitize_name(qid)
            # Pull up to max(K) candidates
            max_k = max(ks) if ks else 1
            cands = first_k_candidates(obj, args.key_field_list, args.key_field, max_k)

            print(f"➡️  {qid}: {len(cands)} candidate(s)", flush=True)
            results_by_k = {}
            any_success_prefix = [False] * (max_k + 1)  # 1-indexed convenience

            # Compile each candidate (up to max_k) until success per K
            for i, snippet in enumerate(cands, start=1):
                tname_i = f"{base_name}_k{i}"
                res = compile_one_theory(isa, sid, work_dir_abs, tname_i, snippet, args.timeout)
                ok = bool(res.get("ok"))
                tag = "OK" if ok else ("TIMEOUT" if res.get("timed_out") else "FAIL")
                print(f"   • k={i}: {tag}", flush=True)
                if not ok:
                    # Optional: print errors for debugging
                    print_isabelle_errors(res, prefix="      ")
                    dump_nodes(res, prefix="      ")
                # Mark prefix success if this one succeeded
                if ok:
                    for p in range(i, max_k + 1):
                        any_success_prefix[p] = True
                    # We still continue compiling next i to collect full logs if desired,
                    # but if you prefer to stop early, you can break here.

            # Update Pass@K counters
            for k in ks:
                if any_success_prefix[k]:
                    pass_at[k] += 1
                results_by_k[f"pass@{k}"] = any_success_prefix[k]

            # log entry
            logf.write(json.dumps({
                "id": qid,
                "num_candidates": len(cands),
                "results": results_by_k
            }) + "\n")

    # Summary
    print("\n====== PASS@K SUMMARY ======")
    print(f"Total problems: {totals}")
    for k in ks:
        pct = (pass_at[k] / totals * 100.0) if totals else 0.0
        print(f"Pass@{k}: {pass_at[k]}/{totals} = {pct:.2f}%")

    if not args.keep_open:
        isa.shutdown()
        print("👋 Client connection closed.")
    else:
        print("🔌 Keeping client connection open.")

if __name__ == "__main__":
    main()
