import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request

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


@app.route("/")
def index():
    return jsonify(
        message="Hello from logtest",
        pod=POD_NAME,
        started=START_TIME,
        now=datetime.now(timezone.utc).isoformat(),
    )


@app.route("/healthz")
def healthz():
    # Quiet-ish endpoint, useful for readiness/liveness probes
    # so you can see probe traffic in the logs separately from real requests.
    return jsonify(status="ok"), 200


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
