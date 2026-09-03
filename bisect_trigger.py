#!/usr/bin/env python3
"""
bisect_trigger —— 从一条「必挂」的真实请求里二分出触发句。

用途：当上游把「按内容拒绝」伪装成 429/限流时，参数维度的 A/B 全都通过，
只能从请求内容本身下手。本脚本把这套定位流程自动化：

  1. 基线   原始请求体重放，确认稳定失败
  2. 对照   最小请求（"hi"）确认稳定成功——排除账号/配额/网络
  3. 消融   依次去掉 tools、压低 max_tokens、把 messages 换成 "hi"，
            定位到底是哪个字段引起的
  4. 二分   在出问题的文本里按行/句二分，逐步缩到最小失败片段
  5. 验证   完整原始请求体，只把这一片段替换掉，确认转为成功

关键：上游一旦返回真实限流，会连累后续请求（账号冷却轮换），
所以每次探测之间必须留足间隔，且把「限流噪声」判为 inconclusive 后重试，
否则会把噪声当成信号，得出完全相反的结论。

用法：
  set AG_PROBE_KEY=...            (Windows)   / export AG_PROBE_KEY=...  (POSIX)
  python bisect_trigger.py --body failing.json --url https://HOST/v1/chat/completions

failing.json 是从代理日志里取出的真实请求体（JSON）。
"""
import argparse
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

OK, FAIL, NOISE = "OK", "FAIL", "NOISE"

# 这些是「池子被锁」的噪声，不是内容被拒的信号，必须等待后重试
NOISE_PAT = re.compile(
    r"all accounts limited|accounts exhausted|wait \d+s|no available account",
    re.I)


class Probe:
    def __init__(self, url, key, interval, timeout=90):
        self.url, self.key = url, key
        self.interval, self.timeout = interval, timeout
        self.calls = 0
        self._last = 0.0

    def _wait(self):
        gap = self.interval - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)

    def __call__(self, body, tries=3):
        """返回 (OK|FAIL|NOISE, 状态码, 片段)。限流噪声自动重试。"""
        code, text = 0, ""
        for _ in range(tries):
            self._wait()
            req = urllib.request.Request(
                self.url, data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + self.key,
                         "Content-Type": "application/json"})
            try:
                r = urllib.request.urlopen(req, timeout=self.timeout)
                code, text = r.status, r.read(400).decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                code, text = e.code, e.read().decode("utf-8", "replace")[:400]
            except Exception as e:
                code, text = 0, repr(e)
            finally:
                self._last = time.time()
                self.calls += 1

            if code == 200:
                return OK, code, text[:120]
            if NOISE_PAT.search(text):
                print(f"      (限流噪声, 等 {self.interval}s 重试)")
                continue
            return FAIL, code, text[:120]
        return NOISE, code, text[:120]


def texts_of(body):
    """列出请求体里所有可改写的文本位置，返回 [(setter, 文本)]。"""
    out = []
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            def make(m):
                return lambda v: m.__setitem__("content", v)
            out.append((make(msg), msg["content"]))
    if isinstance(body.get("system"), str):
        out.append((lambda v: body.__setitem__("system", v), body["system"]))
    return out


def split_text(text):
    """优先按行切；只有一行就按句末标点切；再不行按字符对半。"""
    for parts in (text.split("\n"), re.split(r"(?<=[.。;；])\s+", text)):
        parts = [p for p in parts if p.strip()]
        if len(parts) > 1:
            return parts
    mid = len(text) // 2
    return [text[:mid], text[mid:]] if mid else [text]


def bisect_text(probe, body, idx, log):
    """对第 idx 处文本二分，返回最小失败片段。"""
    parts = split_text(texts_of(body)[idx][1])

    while len(parts) > 1:
        half = len(parts) // 2
        chosen = None
        for label, sub in (("前半", parts[:half]), ("后半", parts[half:])):
            trial = copy.deepcopy(body)
            texts_of(trial)[idx][0]("\n".join(sub))
            verdict, code, _ = probe(trial)
            n = sum(len(x) for x in sub)
            print(f"    {label} {len(sub)} 段 / {n} 字符 -> {verdict} ({code})")
            log.append({"stage": "bisect", "part": label, "chars": n,
                        "verdict": verdict, "code": code})
            if verdict == FAIL:
                chosen = sub
                break
        if chosen is None:
            print("    两半都不失败 —— 触发条件跨越切分点，停在当前粒度")
            break
        parts = chosen
        if len(parts) == 1:
            deeper = split_text(parts[0])
            if len(deeper) == 1:
                break
            parts = deeper

    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--body", required=True, help="真实失败请求体 JSON 文件")
    ap.add_argument("--url", required=True, help="chat completions 端点")
    ap.add_argument("--key-env", default="AG_PROBE_KEY",
                    help="存放 API key 的环境变量名（不走命令行，避免进 shell 历史）")
    ap.add_argument("--interval", type=float, default=45,
                    help="两次探测最小间隔秒数，默认 45")
    ap.add_argument("--repeat", type=int, default=2, help="基线重复次数")
    ap.add_argument("--replacement", default="You are a helpful coding agent.",
                    help="验证阶段用来替换触发句的中性文本")
    ap.add_argument("--out", default="bisect-report.json")
    args = ap.parse_args()

    key = os.environ.get(args.key_env)
    if not key:
        sys.exit(f"环境变量 {args.key_env} 未设置")

    with open(args.body, encoding="utf-8") as fh:
        body = json.load(fh)
    probe = Probe(args.url, key, args.interval)
    log = []

    def dump(obj):
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)

    print(f"\n[1/5] 基线：原始请求体重放 x{args.repeat}")
    for i in range(args.repeat):
        verdict, code, snip = probe(body)
        print(f"  第 {i + 1} 次 -> {verdict} ({code}) {snip}")
        log.append({"stage": "baseline", "verdict": verdict, "code": code})
        if verdict == OK:
            sys.exit("原始请求成功了，不是稳定复现；先确认失败条件再跑本脚本")

    print("\n[2/5] 对照：最小请求")
    minimal = {k: v for k, v in body.items() if k not in ("messages", "system")}
    minimal["messages"] = [{"role": "user", "content": "hi"}]
    verdict, code, snip = probe(minimal)
    print(f"  -> {verdict} ({code}) {snip}")
    log.append({"stage": "control", "verdict": verdict, "code": code})
    if verdict != OK:
        sys.exit("最小请求也失败 —— 是账号/配额/网络问题，不是内容问题")

    print("\n[3/5] 消融：逐个字段")
    ablations = []
    if body.get("tools"):
        b = copy.deepcopy(body)
        b.pop("tools", None)
        ablations.append(("去掉 tools", b))
    if body.get("max_tokens"):
        b = copy.deepcopy(body)
        b["max_tokens"] = 4096
        ablations.append(("max_tokens=4096", b))
    b = copy.deepcopy(body)
    b["messages"] = [{"role": "user", "content": "hi"}]
    b.pop("system", None)
    ablations.append(("messages/system 换成 hi", b))

    content_is_cause = False
    for label, trial in ablations:
        verdict, code, _ = probe(trial)
        print(f"  {label:24s} -> {verdict} ({code})")
        log.append({"stage": "ablation", "label": label,
                    "verdict": verdict, "code": code})
        if label.startswith("messages") and verdict == OK:
            content_is_cause = True

    if not content_is_cause:
        print("\n把 messages 换掉后仍然失败 —— 触发点不在消息文本里，"
              "改查 tools / 请求头 / 模型名")
        dump(log)
        return 0

    print("\n[4/5] 二分：定位到具体文本")
    culprit_idx = None
    for idx, (_, text) in enumerate(texts_of(body)):
        trial = copy.deepcopy(body)
        for j, (setter, _) in enumerate(texts_of(trial)):
            if j != idx:
                setter("You are an assistant.")
        verdict, code, _ = probe(trial)
        print(f"  只保留第 {idx} 处文本({len(text)} 字符) -> {verdict} ({code})")
        log.append({"stage": "isolate", "index": idx, "chars": len(text),
                    "verdict": verdict, "code": code})
        if verdict == FAIL:
            culprit_idx = idx
            break

    if culprit_idx is None:
        print("  没有单独一处文本能复现 —— 可能是多处组合触发")
        dump(log)
        return 0

    frag = bisect_text(probe, body, culprit_idx, log)
    print(f"\n  最小失败片段({len(frag)} 字符):\n  ---\n{frag}\n  ---")

    print("\n[5/5] 验证：完整请求体，只替换这一片段")
    verify = copy.deepcopy(body)
    setter, text = texts_of(verify)[culprit_idx]
    setter(text.replace(frag, args.replacement))
    verdict, code, snip = probe(verify)
    print(f"  -> {verdict} ({code}) {snip}")
    log.append({"stage": "verify", "verdict": verdict, "code": code})

    dump({"trigger": frag, "verified": verdict == OK,
          "probe_calls": probe.calls, "log": log})

    if verdict == OK:
        print(f"\n确认：替换这句即可恢复。共发起 {probe.calls} 次探测。")
        print("把它加进 proxy.py 的 DEFAULT_RULES 或 rules.json 即可。")
    else:
        print("\n只替换这句仍失败 —— 还有别的触发点，以本轮结果为起点继续二分。")
    print(f"报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
