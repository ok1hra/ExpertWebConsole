#!/usr/bin/env python3
"""
Checks that --bundle produces a standalone file that really runs on its own.

The bundle is the only place where the page exists twice, so this guards the
one thing that can silently go wrong: the copy differing from index.html.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "expert_console.py")
PORT = 8079
passed = failed = 0


def note(cond, what):
    global passed, failed
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    passed += bool(cond)
    failed += (not cond)


tmp = tempfile.mkdtemp(prefix="expert-bundle-")
try:
    bundle = os.path.join(tmp, "expert-standalone.py")
    r = subprocess.run([sys.executable, SRC, "--bundle", bundle],
                       capture_output=True, text=True, cwd=ROOT)
    note(r.returncode == 0 and os.path.exists(bundle),
         "--bundle wrote a file" + ("" if r.returncode == 0 else ": " + r.stderr[:80]))
    note(os.access(bundle, os.X_OK), "the bundle is executable")

    # The folded-in page must match index.html byte for byte
    spec = importlib.util.spec_from_file_location("bundled", bundle)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    orig = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    note(mod.BUNDLED_HTML == orig,
         "the folded-in page is byte-identical to index.html")
    note(hashlib.sha256(orig.encode()).hexdigest()
         == hashlib.sha256(mod.BUNDLED_HTML.encode()).hexdigest(), "same checksum")

    # It has to run without index.html anywhere near it
    note(not os.path.exists(os.path.join(tmp, "index.html")),
         "no index.html sits next to the bundle")
    p = subprocess.Popen([sys.executable, bundle, "--simulate",
                          "--http-port", str(PORT)],
                         cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    try:
        page = ""
        for _ in range(40):
            time.sleep(0.1)
            try:
                page = urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/", timeout=1).read().decode()
                break
            except OSError:
                continue
        note("<title>EXPERT 1K-FA" in page, f"it serves the page ({len(page)} B)")
        h = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/health", timeout=2).read())
        note(h.get("source", "").startswith("simulation"), "and answers /health")
    finally:
        p.terminate()
        p.wait(timeout=5)

    # A bundle must refuse to bundle itself again
    r = subprocess.run([sys.executable, bundle, "--bundle",
                        os.path.join(tmp, "again.py")],
                       capture_output=True, text=True, cwd=tmp)
    note(r.returncode != 0 and "already a bundle" in (r.stdout + r.stderr),
         "a bundle refuses to bundle itself again")

    # The source without index.html must fail at startup, not on first request
    lone = os.path.join(tmp, "lone.py")
    shutil.copy(SRC, lone)
    r = subprocess.run([sys.executable, lone, "--simulate",
                        "--http-port", str(PORT + 1)],
                       capture_output=True, text=True, cwd=tmp, timeout=20)
    out = r.stdout + r.stderr
    note(r.returncode != 0 and "index.html not found" in out,
         "the source fails at startup when index.html is missing")
    note("--bundle" in out, "and points at --bundle as the way out")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{passed} proslo, {failed} selhalo")
sys.exit(1 if failed else 0)
