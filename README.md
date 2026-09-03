# ag-rewrite

Google Antigravity 反代拿来跑 Claude 桌面版 / Claude Code 时，**每一条请求都失败**，客户端显示「请求失败，正在重试」。本仓库是原因和修法。

## 现象

- 客户端模型列表正常显示，一发起对话就失败
- 上游报 `429 RESOURCE_EXHAUSTED`，账号池很快全部锁死，对外表现为 `All accounts limited` / `All accounts exhausted`
- 换账号、加账号、降并发、减重试层数、关流式 —— 全都没用
- 自己手搓的请求永远成功，客户端发出的请求永远失败

## 原因

**Google Antigravity 后端会按内容识别并拒绝声明自己是 Claude Agent SDK 的请求，并把这个拒绝伪装成 `429 RESOURCE_EXHAUSTED` 返回。**

触发句就是 Claude 桌面版系统提示词的第一句：

```
You are a Claude agent, built on Anthropic's Claude Agent SDK.
```

完整 70 KB 请求体里**只替换这一句**，其余一字不动，就从必挂变成必通（多次复现确认）。

几个容易踩空的点：

- **不是限流。** 账号池、并发数、重试层数、模型映射、流式与否全都不是原因，只是伪装 429 的下游噪声。
- **是整句的组合特征，不是单个关键词。** `You are a Claude agent.`、`You are an agent built on Anthropic's Agent SDK.`、`You are a helpful agent, built on the Claude Agent SDK.` 全部正常返回 200。
- **伪装的 429 带 30 秒重置延时**，中间层会当成真限流去标记账号冷却、轮换、重试，于是两个账号迅速全锁，症状被放大成「配额不够」。

## 方案

在网关与上游反代之间插一层请求体改写代理，转发前把触发句换成中性表述：

```
客户端 → 网关(new-api 等) → ag-rewrite:8046 → antigravity-manager:8045 → Google
```

只改 `messages` / `system` 里的文本，`tools`、参数、其余内容一律不动；任何异常退化为原样透传，绝不阻断请求。

## 部署

```bash
git clone https://github.com/<you>/ag-rewrite.git /opt/ag-rewrite
sudo cp /opt/ag-rewrite/ag-rewrite.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ag-rewrite
```

然后把网关渠道的 base_url 从上游地址（`http://127.0.0.1:8045`）改成 `http://127.0.0.1:8046`。

配置全走环境变量，在 service 文件里改：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AG_UPSTREAM` | `http://127.0.0.1:8045` | 上游反代地址 |
| `AG_LISTEN_HOST` / `AG_LISTEN_PORT` | `127.0.0.1` / `8046` | 监听地址 |
| `AG_TIMEOUT` | `300` | 上游超时（秒） |
| `AG_LOG` | stderr | 日志文件路径 |
| `AG_RULES` | 内置 | 规则文件，见 `rules.example.json` |

## 运维

触发句取自特定客户端版本的系统提示词，**客户端升级后可能变化**。再次出现失败时查日志：

```
2026-09-03 05:16:30,442 POST /v1/chat/completions hits=1 -> 200
```

- `hits=1` → 改写生效，问题在别处
- `hits=0` 且上游 429 → 出现新触发句

新触发句用 `bisect_trigger.py` 定位（自动做基线/对照/消融/二分/验证，会自己规避限流噪声）：

```bash
export AG_PROBE_KEY=...
python bisect_trigger.py --body failing.json --url https://HOST/v1/chat/completions
```

`failing.json` 从代理日志里取一条真实失败请求体即可。定位到的句子加进 `rules.json` 或 `DEFAULT_RULES`。

## 测试

```bash
python test_rewrite.py     # 13 项，覆盖分块 content、Anthropic 顶层 system、非法输入、tools 不被误改
```

## 说明

本工具用于自有账号的自建网关链路。绕过服务商的内容限制可能与其服务条款冲突，自行评估。代码只做字符串替换，不涉及任何凭据处理。

MIT License.
