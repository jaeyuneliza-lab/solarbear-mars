"""Solar Bear bridge.

Runs the Upstage Studio workflow (Document Agent API v2) on a PDF, stores the
result, writes a notification for each case, and serves the service page so the
page can show live runs. Core judgement stays in the Studio workflow; this file
only uploads, waits, stores, displays and notifies.

Python standard library only. PyMuPDF is used if installed to pull the plume
image out of the MARS notification page; without it the case shows no image.

Usage:  python bridge.py            then open http://127.0.0.1:8770
Config: .env next to this file (see .env.example). Without UPSTAGE_API_KEY the
        bridge runs in mock mode and replays stored setting #22 outputs.
"""
import base64
import datetime as dt
import http.server
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(HERE / ".env")
API = os.environ.get("UPSTAGE_API_BASE", "https://api.upstage.ai/v2").rstrip("/")
KEY = os.environ.get("UPSTAGE_API_KEY", "").strip()
AGENT = os.environ.get("STUDIO_AGENT_ID", "agt_FrfTdFmra9xf9j8GgAp6AH")
CONFIG = os.environ.get("STUDIO_CONFIG_ID", "cfg_3tnXM8PPYfvAutuYiGoLrE")  # setting #22
CONFIG_LABEL = os.environ.get("STUDIO_CONFIG_LABEL", "#22")
MOCK = os.environ.get("MOCK", "").lower() in ("1", "true", "yes") or not KEY
PORT = int(os.environ.get("PORT", "8770"))
WEBHOOK = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
POLL_S = 3
JOB_LIMIT_S = 20 * 60
MAX_PDF = 50 * 1024 * 1024

RESULTS = HERE / "results"
NOTIFS = HERE / "notifications.jsonl"
# the service page: static/ when present, else the site root one level up (repo layout)
STATIC = HERE / "static" if (HERE / "static" / "index.html").exists() else HERE.parent
RESULTS.mkdir(exist_ok=True)

RUNS = {}
RUNS_LOCK = threading.Lock()


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------- Studio API ----------

class StudioError(Exception):
    pass


def api(method, path, body=None, ctype="application/json", timeout=120):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + KEY)
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise StudioError(f"{method} {path} -> HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise StudioError(f"{method} {path} -> {e.reason}") from None


def upload_pdf(pdf, name, run):
    boundary = "----solarbear" + uuid.uuid4().hex
    safe = re.sub(r'[\r\n"]', "_", name)
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nuser_data\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\nContent-Type: application/pdf\r\n\r\n'.encode(),
        pdf,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    f = api("POST", "/files", b"".join(parts), "multipart/form-data; boundary=" + boundary, timeout=300)
    fid = f.get("id")
    if not fid:
        raise StudioError("upload returned no file id: " + json.dumps(f)[:300])
    run["file_id"] = fid
    # page conversion runs after upload; a job on a PROCESSING file is refused (409)
    t0 = time.time()
    while time.time() - t0 < 300:
        st = api("GET", f"/files/{fid}?view=status&pages=none")
        s = str(st.get("status", "")).upper()
        if s in ("UPLOADED", "READY", ""):
            # The workflow reads only files in its Studio file list. An upload whose
            # content matches an earlier file points at it via source_file_id, and
            # repeat uploads chain (new -> earlier upload -> Studio copy): follow to the end.
            root, seen = fid, {fid}
            src = st.get("source_file_id")
            while src and src not in seen and len(seen) < 10:
                root = src
                seen.add(src)
                src = api("GET", f"/files/{src}?view=status&pages=none").get("source_file_id")
            run["file_id"] = root
            return root
        if s == "FAILED":
            raise StudioError("file conversion failed: " + json.dumps(st)[:300])
        time.sleep(3)
    raise StudioError("file still processing after 5 minutes")


def run_job(fid, run):
    body = {
        "model": AGENT,
        "input": [{"role": "user", "content": [{"type": "input_file", "file_id": fid}]}],
        "config_id": CONFIG,
        "background": True,
        "include": ["all"],
    }
    try:
        job = api("POST", "/responses", body)
    except StudioError as e:
        if "No access to file" in str(e):
            raise StudioError("This PDF is not in the workflow's Studio file list. Add it to the file list in "
                              "Studio first, then run it here again (the bridge matches it by content).") from None
        raise
    jid = job.get("id")
    if not jid:
        raise StudioError("job creation returned no id: " + json.dumps(job)[:300])
    run["job"] = jid
    t0 = time.time()
    while job.get("status") in ("queued", "in_progress", "cancelling", None):
        if time.time() - t0 > JOB_LIMIT_S:
            raise StudioError(f"job {jid} still running after {JOB_LIMIT_S // 60} minutes")
        time.sleep(POLL_S)
        job = api("GET", f"/responses/{jid}?include=all")
        run["studio_status"] = job.get("status")
        run["stages"] = stage_view(job)
    if job.get("status") != "completed":
        raise StudioError(f"job {jid} {job.get('status')}: {json.dumps(job.get('error'))[:300]}")
    run["stages"] = stage_view(job)
    return job


# ---------- node progress for the page ----------

# The setting #22 graph in order; key, label, how an output item is recognised (step_type or a name fragment)
STAGES = [
    ("parse", "Parse documents", "document-parse"),
    ("classify", "Classify document types", "document-classify"),
    ("extract", "Extract fields", "information-extract"),
    ("merge", "Merge the case file", "merge"),
    ("review", "Review claims C1–C8", "증빙 검토"),
    ("final", "Decide adequacy", "최종 판정"),
    ("follow", "Draft the follow-up", "후속 문서"),
    ("validate", "Validate before release", "validate"),
]


def stage_of(item):
    t, name = item.get("step_type") or "", item.get("model") or ""
    for key, _, sign in STAGES:
        if t == sign or (t == "instruct" and sign in name):
            return key
    return None


def stage_detail(key, items):
    texts = [c.get("text") for it in items for c in it.get("content") or []]
    objs = [d for t in texts for o in loose_json(t) for d in flatten(o)]
    if key == "parse":
        pr = next((it.get("progress") for it in items if it.get("progress")), None)
        return f"{pr['total']} pages" if pr and pr.get("total") else ""
    if key == "classify":
        kinds = sorted({t for t in texts if isinstance(t, str) and re.fullmatch(r"[a-z_]+", t)})
        return f"{len(kinds)} document types" if kinds else ""
    if key == "extract":
        return f"{len(items)} documents"
    if key == "review":
        claims = next((d["claims"] for d in objs if isinstance(d.get("claims"), list)), [])
        if claims:
            bad = sum(1 for c in claims if str(c.get("status", "")).lower() == "contradicted")
            return f"{len(claims)} claims · {bad} contradicted"
    if key == "final":
        d = next((d for d in objs if "overall_status" in d and "C1_status" in d), None)
        return d["overall_status"] if d else ""
    if key == "follow":
        d = next((d for d in objs if "follow_up_type" in d), None)
        return d["follow_up_type"] if d else ""
    return ""


def stage_view(job):
    """[{key, label, state: pending|running|done, secs, detail, cached}] from the outputs so far."""
    groups = {}
    for it in job.get("output") or []:
        k = stage_of(it)
        if k:
            groups.setdefault(k, []).append(it)
    out, running_seen = [], False
    finished = job.get("status") == "completed"
    for key, label, _ in STAGES:
        items = groups.get(key, [])
        done = bool(items) and (finished or all(it.get("status") == "completed" for it in items))
        st = {"key": key, "label": label, "state": "pending", "secs": None, "detail": "", "cached": False}
        if items:
            st["state"] = "done" if done else "running"
            t0 = min(it.get("created_at") or 0 for it in items)
            t1 = max(it.get("updated_at") or 0 for it in items)
            st["secs"] = max(0, t1 - t0) if t0 else None
            avs = [c.get("additional_values") for it in items for c in it.get("content") or []]
            st["cached"] = any('"cache_hit": true' in a for a in avs if isinstance(a, str))
            st["detail"] = stage_detail(key, items) if done else ""
        out.append(st)
    # the first stage without output is the one Studio is working on now
    if not finished:
        for st in out:
            if st["state"] == "running":
                running_seen = True
                break
            if st["state"] == "pending":
                st["state"] = "running"
                break
    return out


def mock_stages(case, elapsed):
    """Paced stage view for mock mode, using the stored outputs for the details."""
    pace = [2, 2, 3, 1, 6, 3, 3, 1]
    fin, fol, rev = case.get("final") or {}, case.get("follow") or {}, case.get("review") or {}
    claims = rev.get("claims") or []
    details = {
        "parse": "", "classify": f"{len(case.get('ex') or {})} document types",
        "extract": f"{sum(len(v) for v in (case.get('ex') or {}).values())} documents",
        "merge": "", "validate": "",
        "review": f"{len(claims)} claims · {sum(1 for c in claims if str(c.get('status','')).lower() == 'contradicted')} contradicted" if claims else "",
        "final": fin.get("overall_status", ""), "follow": fol.get("follow_up_type", ""),
    }
    out, t = [], 0
    for (key, label, _), d in zip(STAGES, pace):
        st = {"key": key, "label": label, "state": "pending", "secs": None, "detail": "", "cached": False}
        if elapsed >= t + d:
            st.update(state="done", secs=d, detail=details[key])
        elif elapsed >= t:
            st["state"] = "running"
        out.append(st)
        t += d
    return out, t


# ---------- turning step outputs into a case ----------

EXTRACT_SIGNS = {
    "mars_notification": {"emission_rate_t_h", "uncertainty_t_h", "detection_datetime"},
    "operator_email": {"sent_date"},
    "feedback_form": {"ceased", "emissions_cause", "quantification_kg", "actions_taken", "duration_h"},
    "operational_log": {"time_basis", "venting_or_blowdown", "release_start", "maintenance_activity"},
    "field_inspection": {"leak_found", "isolated_time", "equipment_tag"},
    "work_order": {"wo_number", "current_status", "actual_completion_date"},
    "training_record": {"attendee_count"},
}


def loose_json(text):
    """Every JSON object in a step's text (it may be wrapped in prose or code fences)."""
    if not isinstance(text, str):
        return [text] if isinstance(text, (dict, list)) else []
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).rstrip("`").strip()
    try:
        v = json.loads(t)
        # Instruct nodes return their JSON as a JSON-encoded string
        return loose_json(v) if isinstance(v, str) and v.strip()[:1] in "{[" else [v]
    except ValueError:
        pass
    out, depth, start, in_s, esc = [], 0, -1, False, False
    for i, ch in enumerate(t):
        if in_s:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_s = False
            continue
        if ch == '"':
            in_s = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    out.append(json.loads(t[start:i + 1]))
                except ValueError:
                    pass
    return out


def flatten(o):
    if isinstance(o, list):
        for v in o:
            yield from flatten(v)
    elif isinstance(o, dict):
        if set(o) == {"output"}:
            yield from flatten(o["output"])
        else:
            yield o


def classify(objs, case):
    for o in objs:
        for d in flatten(o):
            keys = set(d)
            if "annex_d_record" in keys:
                case["follow"] = d
            elif "C1_status" in keys and "overall_status" in keys:
                case["final"] = d
            elif isinstance(d.get("claims"), list):
                case["review"] = d
            elif "verdict" in keys and "checks" in keys:
                case["validate"] = d
            else:
                for doc, sign in EXTRACT_SIGNS.items():
                    if len(keys & sign) >= min(2, len(sign)):
                        case["ex"].setdefault(doc, []).append(d)
                        break


def case_from_job(job):
    case = {"ex": {}}
    steps = []
    for item in job.get("output") or []:
        for c in item.get("content") or []:
            steps.append({"step_id": c.get("step_id"), "step_index": c.get("step_index")})
            classify(loose_json(c.get("text")), case)
            if c.get("additional_values"):
                classify(loose_json(c.get("additional_values")), case)
    case["steps"] = steps
    return case


def plume_image(pdf):
    """The plume picture on the MARS notification page (Appendix I), as base64 PNG."""
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            return None, ""
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        page = doc[0]
        cap = next((l.strip() for l in page.get_text().splitlines() if "detected by" in l), "")
        cap = cap.replace("ﬁ", "fi").replace("ﬂ", "fl")
        for im in page.get_images(full=True):
            pix = fitz.Pixmap(doc, im[0])
            if pix.width > 600 or pix.height > 600:   # a scanned full page, not the plume
                continue
            smask = im[1]
            if smask:
                pix = fitz.Pixmap(pix, fitz.Pixmap(doc, smask))
            return base64.b64encode(pix.tobytes("png")).decode(), cap
        return None, cap
    except Exception:
        return None, ""


# ---------- storage and notification ----------

def save_case(case):
    cid = re.sub(r"[^A-Za-z0-9_.-]", "_", case["id"])
    (RESULTS / f"{cid}.json").write_text(json.dumps(case, ensure_ascii=False, indent=1), encoding="utf-8")


def load_cases():
    out = []
    for p in sorted(RESULTS.glob("*.json")):
        if p.name.startswith("_"):   # raw responses kept for debugging
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            pass
    return out


def notify(case):
    f = case.get("final") or case.get("follow") or {}
    fu = (case.get("follow") or {}).get("follow_up") or {}
    msg = {
        "time": now_iso(), "case_id": case["id"], "job": case.get("job"),
        "overall_status": f.get("overall_status"), "follow_up_type": (case.get("follow") or {}).get("follow_up_type"),
        "recipient": fu.get("recipient"), "summary": fu.get("summary"),
    }
    with NOTIFS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
    if WEBHOOK:
        text = f"[Solar Bear] {case['id']}: {msg['overall_status']} -> {msg['follow_up_type']} ({msg['recipient']})"
        try:
            req = urllib.request.Request(WEBHOOK, data=json.dumps({"text": text}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            msg["webhook"] = "sent"
        except Exception as e:
            msg["webhook"] = f"failed: {e}"
    return msg


# ---------- a run ----------

def mock_job(name):
    """Replay a stored setting #22 output for the uploaded file (no API key)."""
    lib = json.loads((HERE / "mock_cases.json").read_text(encoding="utf-8"))
    m = re.search(r"DEMO-\d{3}", name or "")
    key = m.group(0) if m and m.group(0) in lib else sorted(lib)[hash(name) % len(lib)]
    return key, lib[key]


def do_run(rid, pdf, name):
    run = RUNS[rid]
    try:
        img, cap = plume_image(pdf)
        if MOCK:
            run.update(state="uploading"); time.sleep(1.5)
            key, stored = mock_job(name)
            run.update(state="studio", studio_status="in_progress")
            t0 = time.time()
            while True:
                run["stages"], total = mock_stages(stored, time.time() - t0)
                if time.time() - t0 >= total:
                    break
                time.sleep(0.5)
            case = {k: stored.get(k) for k in ("ex", "final", "follow", "review")}
            case.update(id=key, job=stored.get("job"), mock=True)
        else:
            run.update(state="uploading")
            fid = upload_pdf(pdf, name, run)
            run.update(state="studio", studio_status="in_progress")
            job = run_job(fid, run)
            case = case_from_job(job)
            fin = case.get("final") or case.get("follow") or {}
            case["id"] = fin.get("case_id") or Path(name).stem
            case["job"] = job.get("id")
            case["usage"] = job.get("usage")
            if not (case.get("final") or case.get("follow")):
                (RESULTS / f"_raw_{job.get('id')}.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
                raise StudioError("the job finished but no decision-node output was found (raw response saved in results/)")
        run.update(state="saving")
        case.update(file=name, t0=run["started"], t1=now_iso(), live=True, config=CONFIG_LABEL)
        if img:
            case["sat"], case["sat_cap"] = img, cap
        save_case(case)
        run["notice"] = notify(case)
        run.update(state="done", case=case)
    except Exception as e:  # report every failure to the page
        run.update(state="error", error=str(e))


# ---------- HTTP ----------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "SolarBearBridge/1.0"

    def log_message(self, fmt, *args):
        print("[bridge]", self.address_string(), fmt % args, flush=True)

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype):
        if not path.exists():
            return self.send_json({"error": "not found"}, 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p in ("/", "/index.html"):
            return self.send_file(STATIC / "index.html", "text/html; charset=utf-8")
        if p == "/favicon.svg":
            return self.send_file(STATIC / "favicon.svg", "image/svg+xml")
        if p == "/api/health":
            return self.send_json({"ok": True, "mode": "mock" if MOCK else "live", "agent": AGENT,
                                   "config": CONFIG, "config_label": CONFIG_LABEL, "webhook": bool(WEBHOOK)})
        if p == "/api/cases":
            return self.send_json(load_cases())
        m = re.fullmatch(r"/api/runs/([a-f0-9]{32})", p)
        if m:
            run = RUNS.get(m.group(1))
            return self.send_json(run if run else {"error": "unknown run"}, 200 if run else 404)
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/run":
            return self.send_json({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_PDF:
            return self.send_json({"error": "send a PDF up to 50 MB"}, 400)
        pdf = self.rfile.read(n)
        if not pdf.startswith(b"%PDF"):
            return self.send_json({"error": "that file is not a PDF"}, 400)
        name = urllib.request.unquote(self.headers.get("X-Filename") or "upload.pdf")
        rid = uuid.uuid4().hex
        with RUNS_LOCK:
            RUNS[rid] = {"id": rid, "file": name, "state": "queued", "started": now_iso(), "mock": MOCK}
        threading.Thread(target=do_run, args=(rid, pdf, name), daemon=True).start()
        return self.send_json({"run": rid, "mock": MOCK})


def main():
    # no address reuse: on Windows it lets a second bridge share the port and answer with stale code
    http.server.ThreadingHTTPServer.allow_reuse_address = False
    http.server.ThreadingHTTPServer.allow_reuse_port = False
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} is already in use: a bridge is probably running already. Close its window first.", flush=True)
        return
    mode = "MOCK (no UPSTAGE_API_KEY; replaying stored setting #22 outputs)" if MOCK else f"LIVE  {AGENT} / {CONFIG} ({CONFIG_LABEL})"
    print(f"Solar Bear bridge on http://127.0.0.1:{PORT}  mode: {mode}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
