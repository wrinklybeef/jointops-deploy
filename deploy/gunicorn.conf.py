"""Linux deployment settings; application source and behavior remain frozen."""

import os
import signal
import threading

bind = "0.0.0.0:8000"
workers = 2
threads = 4
worker_class = "gthread"
worker_connections = 64
backlog = 64
preload_app = False  # Each worker constructs its own SQLAlchemy engine.
reload = False
daemon = False
control_socket_disable = True
timeout = 900  # Worker silence, NOT a per-request timeout with gthread.
graceful_timeout = 1800
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
worker_tmp_dir = "/tmp"
accesslog = "-"
errorlog = "-"
loglevel = "info"
capture_output = True
# Avoid OAuth codes/state and other query-string credentials in access logs.
access_log_format = '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(L)s'
forwarded_allow_ips = ""  # Enable only trusted proxy addresses in the later HTTPS task.

REQUEST_TIMEOUT_SECONDS = 1800


def post_fork(server, worker):
    # Isolate this worker and its PowerShell children from the master/other worker.
    os.setpgid(0, 0)
    worker.jointops_deadlines = {}
    worker.jointops_deadline_lock = threading.Lock()


def _expire_request(worker, key):
    with worker.jointops_deadline_lock:
        if worker.jointops_deadlines.pop(key, None) is None:
            return
    worker.log.critical("Request exceeded the 1800-second deployment deadline; replacing worker group")
    # Killing the isolated worker group also stops its PowerShell subprocesses.
    # Other requests on this worker can be interrupted; never retry writes blindly.
    os.killpg(os.getpid(), signal.SIGKILL)


def pre_request(worker, req):
    key = id(req)
    timer = threading.Timer(REQUEST_TIMEOUT_SECONDS, _expire_request, args=(worker, key))
    timer.daemon = True
    with worker.jointops_deadline_lock:
        worker.jointops_deadlines[key] = timer
    timer.start()


def post_request(worker, req, environ, resp):
    with worker.jointops_deadline_lock:
        timer = worker.jointops_deadlines.pop(id(req), None)
    if timer is not None:
        timer.cancel()
