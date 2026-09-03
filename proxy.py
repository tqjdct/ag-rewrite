#!/usr/bin/env python3
"""
ag-rewrite —— 位于网关与上游反代之间的请求体改写代理。

背景见 README：上游会按内容识别并拒绝某些系统提示词，且把拒绝伪装成
429 RESOURCE_EXHAUSTED。本代理在转发前把触发句替换成中性表述。

设计原则：任何异常都退化为原样透传，绝不阻断请求。

配置（全部走环境变量，均有默认值）：
  AG_UPSTREAM     上游地址        默认 http://127.0.0.1:8045
  AG_LISTEN_HOST  监听地址        默认 127.0.0.1
  AG_LISTEN_PORT  监听端口        默认 8046
  AG_TIMEOUT      上游超时（秒）   默认 300
  AG_LOG          日志文件        默认 stderr
  AG_RULES        规则文件（JSON） 默认使用内置 DEFAULT_RULES
"""
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error

UPSTREAM = os.environ.get("AG_UPSTREAM", "http://127.0.0.1:8045").rstrip("/")
LISTEN = (os.environ.get("AG_LISTEN_HOST", "127.0.0.1"),
          int(os.environ.get("AG_LISTEN_PORT", "8046")))
TIMEOUT = int(os.environ.get("AG_TIMEOUT", "300"))

# 已验证会触发上游「伪装 429」的句子 -> 中性替换。
# 触发句取自具体客户端版本的系统提示词，客户端升级后可能变化，见 README「运维」。
DEFAULT_RULES = [
    ["You are a Claude agent, built on Anthropic's Claude Agent SDK.",
     "You are a helpful coding agent."],
    ["You are Claude Code, Anthropic's official CLI for Claude.",
     "You are a helpful coding agent."],
]


def load_rules():
    """从 AG_RULES 指向的 JSON 加载规则；失败一律退回内置规则。"""
    path = os.environ.get("AG_RULES")
    if not path:
        return [tuple(r) for r in DEFAULT_RULES]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        rules = [(r["match"], r["replace"]) for r in data["rules"]]
        if not rules:
            raise ValueError("规则文件为空")
        return rules
    except Exception as e:
        print(f"[ag-rewrite] 规则文件 {path} 加载失败（{e}），改用内置规则",
              file=sys.stderr)
        return [tuple(r) for r in DEFAULT_RULES]


RULES = load_rules()

_log_file = os.environ.get("AG_LOG")
# basicConfig 不接受 filename 与 stream 同时出现，二选一
_log_kwargs = {"filename": _log_file} if _log_file else {"stream": sys.stderr}
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s",
                    **_log_kwargs)
log = logging.getLogger("ag-rewrite")


def _sub_text(text):
    """对一段文本应用全部替换规则，返回 (新文本, 命中次数)。"""
    hits = 0
    for old, new in RULES:
        if old in text:
            hits += text.count(old)
            text = text.replace(old, new)
    return text, hits


def rewrite(raw):
    """改写请求体。任何解析失败都返回原始字节，调用方无需区分。

    覆盖两种 content 形态：
      - str                      （OpenAI Chat Completions 常见）
      - [{"type":"text","text":…}]（Anthropic / 多模态分块）
    """
    try:
        body = json.loads(raw)
    except Exception:
        return raw, 0
    if not isinstance(body, dict):
        return raw, 0

    total = 0
    targets = []
    if isinstance(body.get("messages"), list):
        targets.extend(body["messages"])
    # 顶层 system 字段（Anthropic Messages 风格）也要覆盖
    if isinstance(body.get("system"), str):
        body["system"], hits = _sub_text(body["system"])
        total += hits
    elif isinstance(body.get("system"), list):
        targets.extend(body["system"])

    for msg in targets:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", msg.get("text"))
        if isinstance(content, str):
            new, hits = _sub_text(content)
            if hits:
                if "content" in msg:
                    msg["content"] = new
                else:
                    msg["text"] = new
                total += hits
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"], hits = _sub_text(block["text"])
                    total += hits

    if not total:
        return raw, 0
    try:
        return json.dumps(body, ensure_ascii=False).encode("utf-8"), total
    except Exception:
        return raw, 0


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 + Connection: close：响应体以关闭连接界定，流式逐块透传，
    # 不需要自己处理 chunked 编码。
    protocol_version = "HTTP/1.0"
    server_version = "ag-rewrite"

    def log_message(self, fmt, *args):
        pass  # 屏蔽 BaseHTTPRequestHandler 的 stderr 噪声，统一用 log

    def _forward(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body, hits = rewrite(raw) if raw else (raw, 0)

            headers = {}
            for k, v in self.headers.items():
                # 这几个头由本代理重新决定，不能照搬
                if k.lower() in ("host", "content-length", "accept-encoding",
                                 "connection", "transfer-encoding"):
                    continue
                headers[k] = v
            headers["Accept-Encoding"] = "identity"  # 拿明文，便于流式转发

            req = urllib.request.Request(
                UPSTREAM + self.path, data=body if body else None,
                headers=headers, method=method)
            resp = urllib.request.urlopen(req, timeout=TIMEOUT)
            status = resp.status
        except urllib.error.HTTPError as e:
            # 上游的错误响应同样要原样回给调用方，不能吞
            resp, status = e, e.code
        except Exception as e:
            log.error("upstream failed: %s", e)
            self.send_response(502)
            self.end_headers()
            try:
                self.wfile.write(str(e).encode())
            except Exception:
                pass
            return

        log.info("%s %s hits=%d -> %s", method, self.path, hits, status)

        self.send_response(status)
        for k, v in resp.headers.items():
            if k.lower() in ("transfer-encoding", "content-length",
                             "connection", "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            while True:
                chunk = resp.read(1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()   # SSE 必须立刻吐，不能攒
        except Exception as e:
            log.warning("stream broken: %s", e)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")


if __name__ == "__main__":
    log.info("ag-rewrite starting on %s:%s -> %s (%d rules)",
             LISTEN[0], LISTEN[1], UPSTREAM, len(RULES))
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
