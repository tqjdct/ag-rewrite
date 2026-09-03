#!/usr/bin/env python3
"""rewrite() 的回归测试。跑法：python test_rewrite.py"""
import json
import sys

import proxy

TRIG = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
SAFE = "You are a helpful coding agent."

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def run(body):
    """返回 (改写后的 dict 或 None, 命中次数)"""
    out, hits = proxy.rewrite(json.dumps(body).encode())
    try:
        return json.loads(out), hits
    except Exception:
        return None, hits


@case("messages 里的 str content 被替换")
def _():
    body, hits = run({"model": "m", "messages": [
        {"role": "system", "content": TRIG},
        {"role": "user", "content": "hi"}]})
    assert hits == 1, hits
    assert body["messages"][0]["content"] == SAFE
    assert body["messages"][1]["content"] == "hi"


@case("触发句嵌在长文本中间也能替换")
def _():
    long = "前缀。\n" + TRIG + "\n后缀。"
    body, hits = run({"messages": [{"role": "system", "content": long}]})
    assert hits == 1
    assert TRIG not in body["messages"][0]["content"]
    assert body["messages"][0]["content"].startswith("前缀。")
    assert body["messages"][0]["content"].endswith("后缀。")


@case("分块 content（[{type,text}]）被替换")
def _():
    body, hits = run({"messages": [{"role": "system", "content": [
        {"type": "text", "text": TRIG},
        {"type": "text", "text": "别动我"}]}]})
    assert hits == 1
    assert body["messages"][0]["content"][0]["text"] == SAFE
    assert body["messages"][0]["content"][1]["text"] == "别动我"


@case("Anthropic 顶层 system 字符串被替换")
def _():
    body, hits = run({"system": TRIG, "messages": [{"role": "user", "content": "hi"}]})
    assert hits == 1
    assert body["system"] == SAFE


@case("Anthropic 顶层 system 数组被替换")
def _():
    body, hits = run({"system": [{"type": "text", "text": TRIG}],
                      "messages": [{"role": "user", "content": "hi"}]})
    assert hits == 1
    assert body["system"][0]["text"] == SAFE


@case("同一句出现多次，命中数正确")
def _():
    body, hits = run({"messages": [
        {"role": "system", "content": TRIG + " " + TRIG},
        {"role": "user", "content": TRIG}]})
    assert hits == 3, hits


@case("无触发句时原样返回、hits=0")
def _():
    raw = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    out, hits = proxy.rewrite(raw)
    assert hits == 0
    assert out is raw, "未命中时必须返回原始字节对象，不重新序列化"


@case("非 JSON 请求体原样透传")
def _():
    raw = b"\x00\x01not json"
    out, hits = proxy.rewrite(raw)
    assert out == raw and hits == 0


@case("JSON 顶层不是对象时原样透传")
def _():
    raw = b'["a","b"]'
    out, hits = proxy.rewrite(raw)
    assert out == raw and hits == 0


@case("messages 里混入非法元素不崩")
def _():
    body, hits = run({"messages": [None, 42, "str",
                                   {"role": "system", "content": TRIG}]})
    assert hits == 1
    assert body["messages"][3]["content"] == SAFE


@case("content 为 None / 数字时跳过不崩")
def _():
    body, hits = run({"messages": [{"role": "user", "content": None},
                                   {"role": "user", "content": 123},
                                   {"role": "system", "content": TRIG}]})
    assert hits == 1


@case("非 ASCII 内容不被转义破坏")
def _():
    body, hits = run({"messages": [{"role": "system", "content": TRIG},
                                   {"role": "user", "content": "中文内容 🎉"}]})
    assert hits == 1
    assert body["messages"][1]["content"] == "中文内容 🎉"


@case("tools 等其余字段一字不动")
def _():
    tools = [{"name": "grep", "description": TRIG}]   # 故意把触发句放进 tools
    body, hits = run({"messages": [{"role": "system", "content": TRIG}],
                      "tools": tools, "max_tokens": 32000, "temperature": 0.7})
    assert hits == 1, "只改 messages/system，不碰 tools"
    assert body["tools"] == tools
    assert body["max_tokens"] == 32000 and body["temperature"] == 0.7


def main():
    failed = 0
    for name, fn in CASES:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}: {e!r}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
