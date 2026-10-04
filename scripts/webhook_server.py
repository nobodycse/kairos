#!/usr/bin/env python3
"""KAIROS Git WebHook 接收器（Python 标准库，无第三方依赖）。

- 监听 :9000，systemd 常驻（kairos-webhook.service），日志走 journald
- 双平台签名兼容：Gitee 校验 X-Gitee-Token（Gitee 的 WebHook「密码」以明文放
  请求头，非 HMAC）；GitHub 校验 X-Hub-Signature-256（HMAC-SHA256 over raw body）
- 仅处理 main 分支 push；并发部署以进程内锁 + deploy.sh 的 flock 串行
- 托管平台等待响应超时很短，先应答 202 再后台执行 deploy.sh
"""
import hashlib
import hmac
import json
import logging
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DEPLOY_SCRIPT = os.environ.get("DEPLOY_SCRIPT", "/opt/kairos/repo/scripts/deploy.sh")
LISTEN_PORT = int(os.environ.get("WEBHOOK_PORT", "9000"))
DEPLOY_TIMEOUT = int(os.environ.get("DEPLOY_TIMEOUT", "3600"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kairos-webhook")

_deploy_lock = threading.Lock()


def _verify(headers, body: bytes) -> bool:
    """GitHub：X-Hub-Signature-256 = HMAC-SHA256；Gitee：X-Gitee-Token 明文密钥。"""
    if not WEBHOOK_SECRET:
        return False
    token = headers.get("X-Gitee-Token")
    if token is not None:
        return hmac.compare_digest(token, WEBHOOK_SECRET)
    signature = headers.get("X-Hub-Signature-256")
    if signature is not None:
        digest = hmac.new(
            WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(f"sha256={digest}", signature)
    return False


def _tail(text: str) -> str:
    return text[-2000:] if text else ""


def _run_deploy():
    if not _deploy_lock.acquire(blocking=False):
        log.warning("上一次部署仍在进行，跳过本次触发")
        return
    try:
        result = subprocess.run(
            ["bash", DEPLOY_SCRIPT],
            capture_output=True,
            text=True,
            timeout=DEPLOY_TIMEOUT,
        )
        if result.returncode == 0:
            log.info("deploy 成功\n%s", _tail(result.stdout))
        else:
            log.error(
                "deploy 失败 rc=%s\nstdout: %s\nstderr: %s",
                result.returncode,
                _tail(result.stdout),
                _tail(result.stderr),
            )
    except subprocess.TimeoutExpired:
        log.error("deploy 超时（%ss），deploy.sh 内部 flock 已保证不叠加执行", DEPLOY_TIMEOUT)
    finally:
        _deploy_lock.release()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self):
        if self.path != "/deploy":
            self._send(404, {"detail": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if not _verify(self.headers, body):
            self._send(401, {"detail": "签名校验失败"})
            return
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"detail": "payload 不是合法 JSON"})
            return
        ref = payload.get("ref", "")
        if ref != "refs/heads/main":
            self._send(200, {"detail": "ignored", "ref": ref})
            return
        threading.Thread(target=_run_deploy, daemon=True).start()
        self._send(202, {"detail": "deploy started"})

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


if __name__ == "__main__":
    if not WEBHOOK_SECRET:
        log.warning("WEBHOOK_SECRET 未配置，所有部署请求将被拒绝")
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log.info("kairos webhook listening on :%s", LISTEN_PORT)
    server.serve_forever()
