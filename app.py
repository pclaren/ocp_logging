import logging
import os
import sys
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

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

LOCAL_TZ = ZoneInfo("Europe/Stockholm")  # auto-handles CET/CEST switchover


class LocalTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=LOCAL_TZ)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S %z")


for handler in logging.getLogger().handlers:
    handler.setFormatter(LocalTimeFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))

# The OpenShift/sclorg Python S2I builder auto-detects app.py and, if it
# exposes a WSGI callable named `application`, runs it with gunicorn
# automatically -- no Dockerfile, no custom run script needed.
application = app

POD_NAME = os.environ.get("HOSTNAME", "unknown-pod")
START_TIME = datetime.now(LOCAL_TZ).isoformat()


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
    body { font-family: system-ui, sans-serif; margin: 3rem; max-width: 40rem; }
    #time { font-size: 1.5rem; font-weight: bold; }
    button { font-size: 1rem; padding: 0.5rem 1rem; margin-top: 1rem; cursor: pointer; }
    textarea { width: 100%; font-family: monospace; font-size: 0.95rem; margin-top: 1rem; }
    #log-status { margin-top: 0.5rem; color: #666; font-size: 0.9rem; }
    hr { margin: 2rem 0; }
    .meta { color: #666; margin-top: 2rem; font-size: 0.85rem; }
  </style>
</head>
<body>
  <h1>logtest</h1>
  <p>Current server time:</p>
  <div id="time">{{ now }}</div>
  <button onclick="refreshTime()">Refresh</button>

  <hr>

  <p>Send arbitrary text (including multi-line) to the container's stdout:</p>
  <textarea id="logtext" rows="6" placeholder="Paste or type anything here, including multiple lines..."></textarea>
  <br>
  <button onclick="sendLog()">Send to stdout</button>
  <div id="log-status"></div>

  <p style="margin-top:2rem;">Or send a fixed sample mimicking a real-world
  timestamp-prefixed multi-line block (e.g. for testing
  <code>detectMultilineException</code>):</p>
  <button onclick="sendSample()">Send SOAP-style sample</button>
  <div id="sample-status"></div>

  <p style="margin-top:2rem;">Or send a genuine Python traceback (the
  shape <code>detectMultilineException</code> is actually designed to
  catch):</p>
  <button onclick="sendTraceback()">Send Python traceback sample</button>
  <div id="traceback-status"></div>

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

    async function sendLog() {
      const text = document.getElementById("logtext").value;
      const status = document.getElementById("log-status");
      if (!text) {
        status.textContent = "Nothing to send.";
        return;
      }
      const resp = await fetch("/api/log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      });
      const data = await resp.json();
      if (resp.ok) {
        status.textContent = `Sent ${data.lines} line(s) to stdout.`;
      } else {
        status.textContent = `Error: ${data.error}`;
      }
    }

    async function sendSample() {
      const status = document.getElementById("sample-status");
      const resp = await fetch("/api/log/sample", { method: "POST" });
      const data = await resp.json();
      if (resp.ok) {
        status.textContent = `Sent ${data.lines}-line sample block to stdout.`;
      } else {
        status.textContent = `Error: ${data.error}`;
      }
    }
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML,
        now=datetime.now(LOCAL_TZ).isoformat(),
        pod=POD_NAME,
        started=START_TIME,
    )


@app.route("/api/time")
def api_time():
    # Hit by the page's Refresh button via fetch(); kept separate from "/"
    # so you get a distinct, easily-filterable log line per refresh click.
    return jsonify(now=datetime.now(LOCAL_TZ).isoformat())


@app.route("/api/log", methods=["POST"])
def api_log():
    """
    Accepts arbitrary (possibly multi-line) text and writes it to stdout
    as a single print() call -- i.e. one process write() containing
    embedded newlines. This is useful for testing whether your log
    pipeline (CRI-O -> Vector/Fluentd -> Loki/Elasticsearch) correctly
    reassembles multi-line output into one log record, vs. splitting it
    into several separate entries.
    """
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    if not text:
        return jsonify(error="empty text"), 400

    lines = text.splitlines()
    marker = uuid.uuid4().hex[:8]

    # Clear BEGIN/END markers via the structured logger, so you can find
    # the block easily in Loki/Kibana even if the raw print() output
    # in between gets split across multiple log entries by the runtime.
    logger.info("BEGIN multiline block id=%s lines=%d", marker, len(lines))
    print(text, flush=True)
    logger.info("END multiline block id=%s", marker)

    return jsonify(status="logged", lines=len(lines), id=marker)


SAMPLE_LOG_TEMPLATE = """{ts} INFO redacted :425 - Signing xml:
Outbound Message
---------------------------
ID: 675873
Address: https://redacted
Encoding: UTF-8
Http-Method: POST
Content-Type: text/xml
Headers: {{Accept=[*/*], SOAPAction=["urn:CorporateService:getCustomerStatement"]}}
Payload: redacted
--------------------------------------
Inbound Message
----------------------------
ID: 675873
Response-Code: 200
Encoding: UTF-8
Content-Type: text/xml;charset=UTF-8
Headers: {{alt-svc=[h3=":443"; ma=86400], cf-cache-status=[DYNAMIC], connection=[keep-alive], content-type=[text/xml;charset=UTF-8]}}
Payload: redacted
--------------------------------------"""


@app.route("/api/log/sample", methods=["POST"])
def api_log_sample():
    """
    Emits a fixed sample block shaped like a real-world timestamp-prefixed
    multi-line log entry (request/response dump), rather than a stack
    trace. Useful for testing whether detectMultilineException (which is
    tuned for exception/stack-trace patterns) also catches this shape, or
    whether it doesn't -- since this is NOT an exception/stack trace.
    Printed as a single stdout write, same as the free-text endpoint.
    """
    ts = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    text = SAMPLE_LOG_TEMPLATE.format(ts=ts)
    lines = text.splitlines()
    print(text, flush=True)
    logger.info("sent SOAP-style sample block, lines=%d", len(lines))
    return jsonify(status="logged", lines=len(lines))


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
