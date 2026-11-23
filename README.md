<<<<<<< HEAD
# Project_1
To contain my primitive programs.
=======
# 多语言代码诊断与修正练习平台

一个基于 **PyQt5 + QScintilla** 的本地桌面应用：上传代码 → 受限执行诊断 → 给出由易到难的三级解决方案 → 按报错类型持久化；同时提供“修正练习”板块，支持真实执行判题、AI 动态出题与 Stack Exchange 官方 API 导题。

---

## 1. 安装与运行

```bash
# 建议 Python 3.9+，进入本项目目录
pip install -r requirements.txt
# 如果 QScintilla 安装失败，可尝试：pip install PyQt5-QScintilla

python main.py
```

`coex` 是可选依赖：装了就优先用它（自带多语言与超时能力）；没装、接口变更或调用失败时，会自动降级到内置的 `subprocess` 受限执行器，程序不会崩溃。

按语言准备外部运行时（只在真正执行该语言代码时需要）：

| 语言 | 需要的命令 |
| --- | --- |
| Python | `python`（使用当前解释器） |
| JavaScript | `node` |
| C / C++ | `gcc` / `g++` |
| Java | `javac` 与 `java`（JDK） |
| Go | `go` |
| Rust | `rustc` |

> 未安装对应运行时不会导致程序崩溃，诊断/判题会明确提示“未检测到运行时/编译器”。

---

## 2. 项目文件结构

| 文件 | 职责 |
| --- | --- |
| `main.py` | 程序入口：Qt 应用初始化、全局样式、启动主窗口 |
| `ui_main.py` | GUI 主体：`QTabWidget` 双标签页、QScintilla 编辑器封装、后台线程、全部交互逻辑 |
| `executor.py` | 安全执行引擎：coex 优先 + subprocess 降级；超时、输出上限、最小环境、资源限制、进程组清理、输出归一 |
| `diagnostics.py` | 诊断模块：`ast.parse` 语法优先检查、按语言解析 stderr 的错误类型/行号、三级解决方案模板 |
| `database.py` | SQLite 持久化：`~/.codetutor/codetutor.db`（诊断/练习历史）与 `~/.codetutor/questions.db`（题库），全部建表语句内置 |
| `question_bank.py` | 题库：静态题种子、AI 兼容接口出题、Stack Exchange v2.3 官方 API 导题、题目可执行性校验 |
| `requirements.txt` | 依赖清单 |
| `README.md` | 本文档 |

数据库位置：`~/.codetutor/`。不写入安装目录，避免权限问题。

---

## 3. 功能说明

### 诊断页
1. 单一入口上传 `.py / .c / .cpp / .java / .js / .go / .rs / .txt`，也可直接在编辑框粘贴。上传后自动识别语言，也能手动改。
2. Python 先用 `ast.parse()` 做语法检查，命中时**不启动外部进程**并给出精确行号；其他语言直接进入执行探测。
3. 代码写入系统临时目录，在独立进程中运行（默认 5 秒超时，可改），捕获 `stdout` / `stderr` / 返回码；从 `stderr` 解析错误类型、行号和消息。
4. 输出三个复杂度递增的方案：**方案一（最小改动）→ 方案二（防御性改写）→ 方案三（重构建议）**。
5. 每次诊断写入 `diagnosis_history`；“历史样例”按钮可按错误类型筛选，查看当时的代码快照与三级方案。
   面板中可用 **样例 ID** 删除单条历史记录：既可在输入框填写 ID，也可先选中表格中的一行（ID 会填入输入框），再点“删除该历史样例”。删除前会二次确认；ID 不存在、格式非法或数据库删除失败分别提示，删除成功后筛选下拉、表格和详情同步刷新。

### 练习页
1. 选择语言后只加载该语言题目（内置 9 道：Python 3、JS 2、C 2、Java 2）。首次运行自动写入 `questions.db`。
2. **开启 AI 更新题库**：调用 OpenAI 兼容的 `/chat/completions`。API Base / 模型 / Key 可在界面填写，或设置环境变量：
   ```bash
   export OPENAI_API_KEY="sk-..."        # Windows PowerShell: $env:OPENAI_API_KEY="sk-..."
   export OPENAI_API_BASE="https://api.openai.com/v1"
   export OPENAI_MODEL="gpt-4o-mini"
   ```
   Key 只存在于内存中的请求头，**不写源码、不写数据库、不回显**。AI 生成的每道题都会用同一个执行引擎验证：正确代码必须能跑且输出等于期望值，含错代码不能刚好通过，否则自动丢弃。
3. **Stack Overflow 官方 API 导入**：使用 Stack Exchange API v2.3，不做 HTML 爬虫；请求前读取 `robots.txt`、解析 `/questions/` 的 `crawl-delay`，串行请求并遵守间隔；导入题目标注来源与原问题链接。网络不可用时静默给出提示，不影响其他功能。
4. 提交作答时运行用户代码，把实际输出按“统一换行 + 去首尾空白”后与期望输出严格比对（同时要求进程成功退出）。正确则通过；错误则在输入框下方展示正确代码。
5. 每次作答都写入 `practice_history`；“作答历史”可查看代码、判定结果与实际输出。
   面板中可按 **题目 ID** 删除指定练习的全部作答历史：输入题目 ID，或先选中该练习的一条记录（题目 ID 会自动填入输入框），再点“删除该练习作答历史”。删除前二次确认；ID 非法、无记录或数据库删除失败分别提示。删除范围严格限定 `question_id`，题目本身、题库配置和其他练习的数据不受影响。

### input() 在非交互环境中的处理

诊断和判题时用户代码运行在独立进程里，原本 `stdin` 是 `DEVNULL`，代码里的
`input()` 会直接抛 `EOFError`。本次改动只处理这一点，不改变业务逻辑、输出格式和已有预期结果：

- 诊断页与练习页新增可选的 **“测试输入”** 输入框，用 `|` 分隔多个值；
  支持整数、浮点数、布尔值、空值和字符串（如 `5|Tom|3.5|true`）。
  这些值作为 stdin 数据送给 `input()`，也可在调用 API 时通过 `inputs=` 传入。
- 对 Python 代码，执行器在临时目录注入一次性的 `sitecustomize.py`，用异常处理包住
  `input()`：有真实标准输入时行为完全不变（包括 prompt 输出）；没有输入时不再抛
  `EOFError`，而是依次使用预定义测试数据，数据用尽后循环回退，最后返回安全默认值（空字符串）。
  用户源码不被改写，因此错误行号保持准确。
- 界面留空时等价于“无预定义输入”，此时依赖 `input()` 的代码会拿到空字符串而不是崩溃；
  原有 9 道静态题都不依赖 `input()`，所以输出和预期结果完全不变。

---

## 4. 安全设计与不可违反约束的落实位置

| 约束 | 实现位置与做法 |
| --- | --- |
| 不使用 `eval()` / `exec()` | `executor.py`：用户代码只写临时文件，交给外部进程；全项目无 `eval`/`exec` 调用 |
| 不无超时/无资源地运行 | `executor.py`：每条路径都有 timeout；POSIX 用启动器设置 `RLIMIT_CPU/AS/FSIZE/CORE`；输出截断 20000 字符；超时后杀整个进程组 |
| 不硬编码密钥 | `question_bank.py`：Key 仅来自界面输入或 `OPENAI_API_KEY`；界面 QLineEdit 为 Password 模式；数据库无密钥字段 |
| 爬虫合规 | `question_bank.py`：只用官方 API；发请求前读 `robots.txt` 并遵守 crawl-delay（不可达时保守 10 秒）；无 HTML 抓取、无并发 |
| 三级解决方案 | `diagnostics.py:build_solutions()` 恒定返回 3 个 `Solution`；即使运行成功也给出质量改进建议 |
| 判题基于实际输出 | `ui_main.py:judge_answer()` + `question_bank.py:validate_question()` 均调用执行引擎比对 stdout |
| 语言白名单 | `executor.py:LANGUAGE_PROFILES`：降级执行器只接受白名单语言，其他直接拒绝 |

执行引擎的关键注释集中在 `_minimal_env()`、`_run()`、`update_questions_from_ai()`、`read_robots_delay()`、`Database._init_schema()`：分别说明为什么必须限制环境变量、必须 `shell=False` + 超时、必须保护密钥、必须先查 robots.txt、必须把数据库放到用户目录，以及绕过它们的风险。

---

## 5. 常见问题

- **QScintilla 导入失败**：程序会降级到 `QPlainTextEdit` 并弹窗提示。按 `pip install PyQt5-QScintilla` 或 `pip install QScintilla` 安装后重启。
- **C/Java 题目总是不通过**：本机缺少 `gcc` / `javac`。安装后重试；诊断结果会明确显示“未检测到运行时/编译器”。
- **AI 更新提示未配置 Key**：填写界面 API Key 或设置 `OPENAI_API_KEY` 环境变量。任意 OpenAI 兼容端点都可通过 API Base 指定。
- **AI 生成很多题被跳过**：这是质量门禁在起作用——正确代码输出不匹配或含错代码也能通过的题目会被丢弃，避免污染题库。
- **题库重复题目**：`questions(language, title)` 有唯一索引，重复更新会安全跳过。
>>>>>>> ee7cc41 (first commit)
