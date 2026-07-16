import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string, request

# --- Logging setup -----------------------------------------------------
# Force logs to stdout, unbuffered, with a structured-ish format.
# This is what `oc logs` / your log-forwarding pipeline (Vector, Fluentd, etc.)
# will actually pick up.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("logtest")

app = Flask(__name__)

# The OpenShift/sclorg Python S2I builder auto-detects app.py and, if it
# exposes a WSGI callable named `application`, runs it with gunicorn
# automatically -- no Dockerfile, no custom run script needed.
application = app

POD_NAME = os.environ.get("HOSTNAME", "unknown-pod")
START_TIME = datetime.now(timezone.utc).isoformat()


@app.before_request
def log_request():
    request.request_id = str(uuid.uuid4())[:8]
    logger.info(
        "REQUEST id=%s method=%s path=%s remote=%s",
        request.request_id,
        request.method,
        request.path,
        request.remote_addr,
    )


@app.after_request
def log_response(response):
    rid = getattr(request, "request_id", "-")
    logger.info(
        "RESPONSE id=%s status=%s",
        rid,
        response.status_code,
    )
    # Also a plain print(), to prove both stdlib logging and print()
    # end up in the container's stdout stream.
    print(f"[print] handled {request.method} {request.path} -> {response.status_code}", flush=True)
    return response


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>logtest</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 3rem; }
    #time { font-size: 1.5rem; font-weight: bold; }
    button { font-size: 1rem; padding: 0.5rem 1rem; margin-top: 1rem; cursor: pointer; }
    .meta { color: #666; margin-top: 2rem; font-size: 0.85rem; }
  </style>
</head>
<body>
  <h1>logtest</h1>
  <p>Current server time:</p>
  <div id="time">{{ now }}</div>
  <button onclick="refreshTime()">Refresh</button>

  <div class="meta">
    pod: {{ pod }}<br>
    started: {{ started }}
  </div>

  <script>
    async function refreshTime() {
      const resp = await fetch("/api/time");
      const data = await resp.json();
      document.getElementById("time").textContent = data.now;
    }
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML,
        now=datetime.now(timezone.utc).isoformat(),
        pod=POD_NAME,
        started=START_TIME,
    )


@app.route("/api/time")
def api_time():
    # Hit by the page's Refresh button via fetch(); kept separate from "/"
    # so you get a distinct, easily-filterable log line per refresh click.
    return jsonify(now=datetime.now(timezone.utc).isoformat())


@app.route("/healthz")
def healthz():
    # Generic/manual check - not wired to a specific probe.
    return jsonify(status="ok"), 200


@app.route("/healthz/live")
def liveness():
    # Liveness: only fail if the process itself is stuck/deadlocked.
    # Keep this cheap and dependency-free -- OpenShift will restart the
    # pod if this fails, which doesn't help if the problem is a slow
    # downstream dependency rather than the process itself.
    return jsonify(status="alive"), 200


@app.route("/healthz/ready")
def readiness():
    # Readiness: fail if the app isn't ready to serve traffic yet
    # (e.g. still warming up, or a dependency is unreachable).
    # This is a stub -- wire in real checks (DB ping, cache, etc.) as needed.
    ready = True
    if not ready:
        logger.warning("readiness check failed")
        return jsonify(status="not ready"), 503
    return jsonify(status="ready"), 200


@app.route("/log/<level>")
def log_level(level):
    """Trigger a log line at a given level: debug, info, warning, error, critical."""
    msg = f"manual log line triggered via /log/{level}"
    level = level.lower()
    if level == "debug":
        logger.debug(msg)
    elif level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    elif level == "critical":
        logger.critical(msg)
    else:
        return jsonify(error=f"unknown level '{level}'"), 400
    return jsonify(triggered=level, message=msg)


@app.route("/burst/<int:count>")
def burst(count):
    """Emit `count` log lines quickly - handy for testing log volume/throughput."""
    count = max(1, min(count, 1000))
    for i in range(count):
        logger.info("burst line %d/%d", i + 1, count)
    return jsonify(emitted=count)


@app.route("/crash")
def crash():
    """Deliberately raise an unhandled exception, to see a traceback in stdout/stderr."""
    logger.error("about to raise a deliberate exception for testing")
    raise RuntimeError("deliberate test crash from /crash endpoint")


@app.route("/env")
def show_env():
    """Dump a few relevant env vars/pod info - useful when checking log metadata/labels."""
    interesting = {k: v for k, v in os.environ.items() if k.startswith(("HOSTNAME", "POD_", "KUBERNETES_", "OPENSHIFT_"))}
    return jsonify(pod_name=POD_NAME, env=interesting)


if __name__ == "__main__":
    logger.info("starting logtest app on pod %s", POD_NAME)
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
