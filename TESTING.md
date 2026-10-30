# 测试与配置指南

> 面向本机（Windows 11 + PowerShell）。所有命令都在 **D:\travel-planner** 下执行。

---

## 零、先解决一个坑：`PYTHONPATH`

项目是 `src/` 布局，**直接 `python -m travel_planner...` 会报 ModuleNotFoundError**。
三选一：

```powershell
# 方式 A（推荐，每个新开的 PowerShell 窗口执行一次）
cd D:\travel-planner
$env:PYTHONPATH = "src"

# 方式 B：不设环境变量，每次加前缀
cd D:\travel-planner
$env:PYTHONPATH="src"; python -m travel_planner.api.demo serve

# 方式 C：正式安装（一次即可，之后任何目录都能跑）
cd D:\travel-planner
pip install -e .
```

下面所有命令都假设你已经执行过方式 A。

---

## 一、加 Key（3 分钟）

### 1.1 先选一个模型平台

项目走 **OpenAI 兼容协议**，所以任何兼容平台的 key 都能用，只改两行配置：

| 平台 | `OPENAI_BASE_URL` | `MODEL` 填什么 | 说明 |
|---|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | **国内可直连，最推荐**；`deepseek-reasoner` 是推理模型，慢但更强 |
| 阿里百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | 国内直连 |
| 月之暗面 | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` | 国内直连 |
| OpenAI 官方 | `https://api.openai.com/v1` | `gpt-4o-mini` | 国内需代理 |

> 本项目只用了 `with_structured_output`（结构化 JSON 输出）和普通 chat，
> 上面几个平台都支持。**不需要**开通特殊权限。

### 1.2 建 `.env` 文件

在 **D:\travel-planner** 下新建一个文件，名字就叫 `.env`（注意前面有个点，没有扩展名）：

```ini
OPENAI_API_KEY=sk-你的真实key
OPENAI_BASE_URL=https://api.deepseek.com/v1
MODEL=deepseek-chat
```

**三种建法，任选一种：**

**方法一：记事本（最直观）**

```powershell
cd D:\travel-planner
notepad .env
```
在记事本里粘贴上面三行 → 改掉 key → `Ctrl+S` 保存 → 关掉。
如果记事本问"是否保存为 .env.txt"，选 **是**，然后把文件改名去掉 `.txt`。

**方法二：PowerShell 一行命令**

```powershell
cd D:\travel-planner
@"
OPENAI_API_KEY=sk-你的真实key
OPENAI_BASE_URL=https://api.deepseek.com/v1
MODEL=deepseek-chat
"@ | Set-Content -Path .env -Encoding utf8
```
把 `sk-你的真实key` 换成真 key 再回车。

**方法三：临时环境变量（不落盘，只对当前窗口有效）**

```powershell
cd D:\travel-planner
$env:OPENAI_API_KEY  = "sk-你的真实key"
$env:OPENAI_BASE_URL = "https://api.deepseek.com/v1"
$env:MODEL           = "deepseek-chat"
```
关掉窗口就失效。适合"只想试一次"。

### 1.3 验证 key 生效

```powershell
cd D:\travel-planner
$env:PYTHONPATH="src"
python -m travel_planner.llm.check
```

正常输出长这样：

```
============================================================
LLM 配置检查
============================================================
{
  "configured": true,
  "key_hint": "sk-abc1...9xyz",
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "provider_guess": "deepseek"
}

发起一次真实调用…
  ✔ 调用成功  回复='你好'
     token: in=23 out=2

✔ 全部正常，可以跑 python -m travel_planner.api.demo run
```

看到 `✔ 调用成功` 就说明 key、网络、模型名**全都对**。

### 1.4 报错对照表

| 报错 | 原因 | 怎么办 |
|---|---|---|
| `configured: false` | 没读到 key | `.env` 必须放在 **D:\travel-planner**（不是 D:\，也不是 src\）；变量名必须是 `OPENAI_API_KEY` |
| `401 Authentication Fails` | key 无效/写错/已过期 | 重新复制 key；检查有没有多余空格、引号、`sk-` 前缀重复 |
| `404 model not found` | 模型名不对 | DeepSeek 用 `deepseek-chat`，不要填 `gpt-4o-mini` |
| `ConnectError / timeout` | 网络不通 | 换国内平台（DeepSeek/百炼/月之暗面）；或检查代理设置 |
| `Missing credentials` | 代码读到了空字符串 | 同第一行；另外确认没有写成 `OPENAI_KEY` |
| `余额不足 / Insufficient` | 账户没钱 | 充一点，本项目一次完整规划约 5~10 次 LLM 调用，成本几分钱 |

> **`.env` 已经在 .gitignore 里**，不会被推到 GitHub。但也别手动提交它。

---

## 二、测试（从"不需要 key"到"完整跑通"）

### 2.1 第 0 层：107 项自动化测试（不需要 key、不需要网络）

这是最该先跑的。全部用确定性 LLM 替身，2 秒内出结果：

```powershell
cd D:\travel-planner
$env:PYTHONPATH="src"
python -m pytest -q
```

期望输出：

```
107 passed in 7.40s
```

**按模块单跑**（调某一层时用）：

```powershell
python -m pytest tests/test_api.py -q          # HTTP 接口层（16 项）
python -m pytest tests/test_graph_e2e.py -q    # 图端到端
python -m pytest tests/test_geo_utils.py -q    # 聚类算法
python -m pytest tests/test_review.py -q       # 事实校验
python -m pytest tests/test_mcp_tools.py -q    # MCP 工具层
```

`-v` 可以看每个用例名字，`-x` 遇到第一个失败就停。

### 2.2 第 1 层：工具层验证（不需要 key）

```powershell
python verify_tools.py
```
验证 4 个 MCP 工具返回结构是否稳定、超时重试是否真生效、mock 数据能否支撑完整规划。
结果写在 `verify_out.txt`，期望 **25/25 PASS**。

### 2.3 第 2 层：完整规划一次（不需要 key，走降级）

```powershell
python export_demo.py
```
用确定性 LLM 替身跑一遍北京 3 天，生成 `GRAPH_AND_DEMO.md`（mermaid 图 + 真实 trace）。

### 2.4 第 3 层：起 HTTP 服务（HTTP 全流程）

**方式一：一键自验（自己起服务、跑完 8 组场景、退出）**

```powershell
python -m travel_planner.api.demo run
```
会依次验证：提交 202、SSE 事件流、查结果、trace、404 信封、422 校验、SSE 404、OpenAPI。

**方式二：起服务后用 curl 手动打（推荐用来熟悉接口）**

```powershell
# 窗口 1：起服务（保持这个窗口不关）
python -m travel_planner.api.demo serve --port 8000
```

```powershell
# 窗口 2：打接口
cd D:\travel-planner

# ① 提交任务（会返回 task_id，记下来）
curl.exe -s -X POST http://127.0.0.1:8000/plan `
  -H "Content-Type: application/json" `
  -d "{\"destination\":\"北京\",\"start_date\":\"2026-11-01\",\"days\":3,\"travelers\":1,\"total_budget\":3000,\"preferences\":[\"历史\",\"美食\"],\"transport_pref\":\"public\"}"

# ② 订阅进度（-N 必须加，否则 PowerShell 会缓冲住看不见）
curl.exe -N http://127.0.0.1:8000/plan/<把task_id粘这里>/stream

# ③ 看结果
curl.exe -s http://127.0.0.1:8000/plan/<task_id>

# ④ 看每节点耗时
curl.exe -s http://127.0.0.1:8000/plan/<task_id>/trace
```

**方式三：浏览器（最省事）**

起服务后打开 <http://127.0.0.1:8000/docs> —— Swagger 可交互文档，
点 `POST /plan` → `Try it out` → 改请求体 → `Execute`，直接看返回。

### 2.5 第 4 层：接真实 LLM 跑

配好 key（第一步）后，重复 2.3 / 2.4，这次走真模型：

```powershell
python -m travel_planner.api.demo run
```

区别：耗时从 ~0.2 秒变成 10~40 秒（每次 LLM 调用要网络往返），
`trace` 里各节点耗时会明显变大，`status` 通常是 `ok` 而不是 `degraded`。

**快速判断有没有走真模型**：看 trace 里的 `status`。
`degraded` / `llm_error` = 降级了（key 或网络有问题）；`ok` = 真跑通了。

### 2.6 判定标准：怎么算"测试通过了"

| 层级 | 通过标准 |
|---|---|
| 自动化测试 | `107 passed`，零 failed、零 warning |
| 工具层 | `25/25 PASS` |
| HTTP 层 | POST 返回 202 + task_id；SSE 收到 `task_start`→多个 `node_end`→`task_end`；GET 结果有 `itinerary.days == 3`；`budget.within_budget == true`；`review.passed == true` |
| 真实 LLM | trace 里 `status=ok` 占多数，且 `review.passed == true` |

---

## 三、常见问题

**Q：`python -m pytest` 报 `no tests ran`？**
A：不在项目根目录。先 `cd D:\travel-planner`。

**Q：`ModuleNotFoundError: No module named 'travel_planner'`？**
A：没设 `PYTHONPATH`。执行 `$env:PYTHONPATH="src"` 或 `pip install -e .`。

**Q：端口被占用（`[Errno 10048]`）？**
A：换端口：`python -m travel_planner.api.demo serve --port 8010`。

**Q：SSE 用 curl 看不到实时输出？**
A：漏了 `-N`。`curl -N` 关掉缓冲；PowerShell 里必须写 `curl.exe`，不能写 `curl`。

**Q：没有 key 能不能跑？**
A：能。测试、工具层、demo 全部用替身/降级，但规划结果会 `status=failed` 并明确提示缺凭证
（这是刻意设计：宁可快速失败说清原因，也不要空转到 recursion_limit 后只报"步数超限"）。

**Q：一次完整规划大概花多少钱？**
A：5~10 次 LLM 调用，输入几千 token、输出几百 token，DeepSeek 约 **1~3 分钱**。
