#!/usr/bin/env python3
import os, re, json, argparse, textwrap, concurrent.futures
from isabelle_client import get_isabelle_client

import concurrent.futures

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
        # Best-effort cancel + do NOT wait for the worker to finish
        fut.cancel()
        ex.shutdown(wait=False, cancel_futures=True)
        return {
            "ok": False,
            "errors": [{"message": {"kind": "ERROR",
                                    "message": f"theory check timed out after {timeout_s:.1f}s"}}],
            "timed_out": True,
        }
    finally:
        # If we didn’t time out, we can shut down normally (wait=True).
        if not timed_out:
            try:
                ex.shutdown(wait=True, cancel_futures=True)
            except Exception:
                pass


# ---------- Helpers for client return-shape quirks ----------
def get_session_id(x):
    return x.get("session_id") if isinstance(x, dict) else x

def coerce_use_theories_result(res):
    # If lib returns a dict, great
    if isinstance(res, dict):
        return res

    # Many versions return a list of frames; try to parse JSON from any of them
    if isinstance(res, list) and res:
        # Try last frame first
        last = res[-1]
        body = getattr(last, "response_body", None)
        if body is None and isinstance(last, dict):
            body = last.get("response_body")
        if isinstance(body, str):
            try:
                return json.loads(body)
            except Exception:
                pass

        # Fall back: scan all frames
        for fr in reversed(res):
            b = getattr(fr, "response_body", None)
            if b is None and isinstance(fr, dict):
                b = fr.get("response_body")
            if isinstance(b, str):
                try:
                    return json.loads(b)
                except Exception:
                    continue

        # Give back raw frames so dump_nodes can show something useful
        frames = []
        for fr in res:
            frames.append({
                "type": getattr(fr, "response_type", getattr(fr, "type", None)),
                "body": getattr(fr, "response_body", getattr(fr, "body", None)),
            })
        return {"ok": False, "frames": frames}

    return {"ok": False, "errors": [{"message": {"kind": "ERROR", "message": f"Unexpected response type: {type(res)}"}}]}

# ---------- Name + theory wrappers ----------
def sanitize_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not s:
        s = "T"
    if not s[0].isalpha():
        s = "T_" + s
    # Capitalize first letter so header, file, and theories entry match
    s = s[0].upper() + s[1:]
    return s[:120]

# ---------- GPT snippet normalization (no regex; drop only the first 'theorem' line) ----------
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

    # Already an Isar statement
    for kw in ("lemma ", "theorem ", "proposition ", "corollary ", "fact "):
        if head_low.startswith(kw):
            return f"{after.rstrip()}\nsorry\n"  # was oops

    # 'shows "..."' — keep inside theorem/sorry
    if head.startswith('shows "'):
        return "theorem\n  " + head.rstrip() + "\n" + "sorry\n"  # was oops

    # Treat as term
    term = normalize_lets(after)
    term_escaped = term.replace('"', '\\"')
    return "theorem\n  shows \"" + term_escaped + "\"\n" + "sorry\n"  # was oops

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
            print(f"{prefix}[ERROR] {e}")
            printed_any = True
            continue
        if not isinstance(e, dict):
            print(f"{prefix}[ERROR] (unexpected error entry: {type(e)}) {e}")
            printed_any = True
            continue
        msg = e.get("message")
        if isinstance(msg, dict):
            kind = msg.get("kind", "ERROR")
            mtxt = msg.get("message", "")
            pos  = msg.get("pos", {})
            print(f"{prefix}[{kind}] {mtxt}")
            p = _fmt_pos(pos)
            if p:
                print(prefix + p)
            printed_any = True
        elif isinstance(msg, (str, bytes)):
            print(f"{prefix}[ERROR] {msg}")
            printed_any = True
        else:
            print(f"{prefix}[ERROR] {e}")
            printed_any = True
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

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="Batch-compile GPT-formal statements via Isabelle Server")
    ap.add_argument("--server-line", required=True)
    ap.add_argument("--json", required=True)
    ap.add_argument("--key-field", default="gpt_formal_s")
    ap.add_argument("--session", default="HOL")
    ap.add_argument("--work-dir", default="isabelle_batch_tmp")
    ap.add_argument("--log", default="compile_log.jsonl")
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--probe", action="store_true", help="Compile a known-good Probe theory first")
    ap.add_argument("--timeout", type=float, default=30.0, help="Per-theory timeout in seconds")
    args = ap.parse_args()

    work_dir_abs = os.path.abspath(args.work_dir)
    os.makedirs(work_dir_abs, exist_ok=True)
    data = json.load(open(args.json, "r", encoding="utf-8"))

    isa = get_isabelle_client(args.server_line)
    print("✅ Connected to Isabelle server")

    # Make session aware of our directory, then start session
    isa.session_build(session=args.session, dirs=[work_dir_abs])
    sid = get_session_id(isa.session_start(session=args.session))
    print("🧠 Session started:", sid)

    # Optional probe to verify pipeline
    if args.probe:
        probe = "Probe"
        with open(os.path.join(work_dir_abs, f"{probe}.thy"), "w", encoding="utf-8") as f:
            f.write(f"""theory {probe}
imports Main
begin
lemma "True" by simp
end
""")
        res_raw = run_with_timeout(
            isa.use_theories, args.timeout,
            session_id=sid,
            theories=[probe],
            master_dir=work_dir_abs,
            unicode_symbols=True,
            watchdog_timeout=int(args.timeout)
        )
        res = coerce_use_theories_result(res_raw)
        print("🔎 Probe OK:", res.get("ok"))
        if not res.get("ok"):
            print_isabelle_errors(res, prefix="   ")
            dump_nodes(res, prefix="   ")

    items = list(data.items())
    if args.limit > 0:
        items = items[:args.limit]

    total = passed = failures = 0

    with open(args.log, "a", encoding="utf-8") as logf:
        for idx, (key, obj) in enumerate(items, start=1):
            total += 1
            snippet = obj.get(args.key_field)
            if not snippet or not isinstance(snippet, str):
                failures += 1
                logf.write(json.dumps({"id": key, "status":"skip-no-snippet"}) + "\n")
                print(f"➡️  {key}: SKIP (no {args.key_field})", flush=True)
                continue

            tname = sanitize_name(key)
            thy_path = os.path.join(work_dir_abs, f"{tname}.thy")
            theory_text = wrap_as_theory(tname, snippet)

            with open(thy_path, "w", encoding="utf-8") as tf:
                tf.write(theory_text)
            if idx == 1:
                print(f"📄 Wrote: {thy_path}", flush=True)

            print(f"➡️  {key}: compiling…", flush=True)
            res_raw = run_with_timeout(
                isa.use_theories, args.timeout,
                session_id=sid,
                theories=[tname],
                master_dir=work_dir_abs,
                unicode_symbols=True,
                watchdog_timeout=int(args.timeout)
            )
            res = coerce_use_theories_result(res_raw)

            ok = bool(res.get("ok"))
            if ok:
                passed += 1
                print(f"✅ {key}: OK", flush=True)
            else:
                failures += 1
                tag = "TIMEOUT" if res.get("timed_out") else "FAIL"
                print(f"❌ {key}: {tag}", flush=True)
                print_isabelle_errors(res, prefix="   ")
                dump_nodes(res, prefix="   ")

            logf.write(json.dumps({
                "id": key,
                "theory": tname,
                "status": "ok" if ok else ("timeout" if res.get("timed_out") else "fail"),
                "ok": ok,
                "timed_out": bool(res.get("timed_out")),
                "errors": res.get("errors", []),
            }) + "\n")

    acc = (passed / total) * 100 if total else 0.0
    print("\n====== SUMMARY ======")
    print(f"Total:   {total}")
    print(f"Passed:  {passed}")
    print(f"Failed:  {failures}")
    print(f"Accuracy: {acc:.2f}%")

    if not args.keep_open:
        isa.shutdown()
        print("👋 Client connection closed.")
    else:
        print("🔌 Keeping client connection open.")

if __name__ == "__main__":
    main()
