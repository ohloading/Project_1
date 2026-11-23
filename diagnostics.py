# -*- coding: utf-8 -*-
"""
diagnostics.py —— 语法检查、错误定位与三级解决方案

流程：
    Python  -> 先 ast.parse() 做零成本语法检查，语法不过不启动子进程
    其他语言 -> 直接进入受限执行器
    执行失败 -> 从 stderr 解析错误类型 / 行号 / 消息
    任何错误 -> 必定产出三个复杂度递增的方案（最小改动 / 防御性改写 / 重构建议）

没有 ast.parse 这一步，Python 语法错误会被迫走完整执行流程，既慢又拿不到
精确行列信息；没有三级方案，诊断结果就退化成"报错原文复读机"。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from executor import SAFE_INPUT_DEFAULT, ExecutionResult, execute_code, normalize_language

# ---------------------------------------------------------------------------
# 错误解析
# ---------------------------------------------------------------------------
@dataclass
class ErrorInfo:
    error_type: str = "未知错误"
    line: Optional[int] = None
    message: str = ""
    raw: str = ""


@dataclass
class Solution:
    level: str
    title: str
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"level": self.level, "title": self.title, "detail": self.detail}


@dataclass
class DiagnosisResult:
    ok: bool
    language: str
    engine: str
    error: Optional[ErrorInfo]
    solutions: List[Solution] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False
    note: str = ""
    syntax_checked: bool = False


def error_from_syntax_error(exc: SyntaxError, code: str) -> ErrorInfo:
    return ErrorInfo(
        error_type=type(exc).__name__,
        line=exc.lineno,
        message=exc.msg or str(exc),
        raw="\n".join((str(exc), exc.text or "")),
    )


def _python_error(stderr: str) -> ErrorInfo:
    error_type, message = "运行时错误", ""
    for line in reversed(stderr.splitlines()):
        match = re.match(
            r"^\s*([\w.]*(?:Error|Exception|Interrupt|Exit|Warning))\s*:\s*(.*)$", line
        )
        if match:
            error_type = match.group(1).split(".")[-1]
            message = match.group(2).strip()
            break
    line_numbers = re.findall(r'File\s+"[^"]+",\s+line\s+(\d+)', stderr)
    line = int(line_numbers[-1]) if line_numbers else None
    if not message:
        message = (stderr.strip().splitlines() or [""])[-1][:400]
    return ErrorInfo(error_type, line, message[:400], stderr)


def _javascript_error(stderr: str) -> ErrorInfo:
    error_type, message = "运行时错误", ""
    match = re.search(r"(?m)^\s*([\w]*Error)\s*:\s*(.+)$", stderr)
    if match:
        error_type, message = match.group(1), match.group(2).strip()
    positions = re.findall(r"([^\s()]+\.js):(\d+):(\d+)", stderr)
    line = int(positions[-1][1]) if positions else None
    if not message:
        message = (stderr.strip().splitlines() or [""])[-1][:400]
    return ErrorInfo(error_type, line, message[:400], stderr)


def _c_error(stderr: str) -> ErrorInfo:
    match = re.search(r"[^\s:]+\.(?:c|h|cpp|cc):(\d+):\d+:\s*(?:fatal\s+)?error:\s*(.+)", stderr)
    if match:
        return ErrorInfo("编译错误", int(match.group(1)), match.group(2).strip()[:400], stderr)
    if "Signal: SIGFPE" in stderr or "Floating point exception" in stderr:
        return ErrorInfo("运行时错误（除零/算术异常）", None, stderr.strip()[:400], stderr)
    if "Segmentation fault" in stderr:
        return ErrorInfo("段错误（内存越界）", None, stderr.strip()[:400], stderr)
    return ErrorInfo("运行时错误", None, (stderr.strip().splitlines() or [""])[-1][:400], stderr)


def _java_error(stderr: str) -> ErrorInfo:
    compile_match = re.search(
        r"[^\s:]+\.java:(\d+):\s*(?:error:)?\s*(.+)", stderr
    )
    if compile_match:
        return ErrorInfo("编译错误", int(compile_match.group(1)),
                         compile_match.group(2).strip()[:400], stderr)
    runtime = re.search(
        r'Exception in thread\s+"[^"]+"\s+([\w.$]+(?:Exception|Error))\s*:\s*(.+)', stderr
    )
    if runtime:
        line_match = re.search(r"at\s+[\w.$]+\([^:)]+\.java:(\d+)\)", stderr)
        return ErrorInfo(
            runtime.group(1).split(".")[-1],
            int(line_match.group(1)) if line_match else None,
            runtime.group(2).strip()[:400], stderr,
        )
    return ErrorInfo("运行时错误", None, (stderr.strip().splitlines() or [""])[-1][:400], stderr)


def _go_error(stderr: str) -> ErrorInfo:
    match = re.search(r"[^\s:]+\.go:(\d+):(\d+):\s*(.+)", stderr)
    if match:
        return ErrorInfo("编译/运行时错误", int(match.group(1)),
                         match.group(3).strip()[:400], stderr)
    return ErrorInfo("运行时错误", None, (stderr.strip().splitlines() or [""])[-1][:400], stderr)


def _rust_error(stderr: str) -> ErrorInfo:
    kind = re.search(r"error(?:\[E\d+\])?\s*:\s*(.+)", stderr)
    position = re.search(r"-->\s*[^\s:]+:(\d+):(\d+)", stderr)
    return ErrorInfo(
        "编译错误",
        int(position.group(1)) if position else None,
        (kind.group(1).strip() if kind else (stderr.strip().splitlines() or [""])[-1])[:400],
        stderr,
    )


def parse_error(stderr: str, language: str) -> ErrorInfo:
    """按语言选择解析规则；解析器拿不到行号时返回 None，UI 显示"未定位"。"""
    lang = normalize_language(language)
    parsers = {
        "python": _python_error,
        "javascript": _javascript_error,
        "c": _c_error,
        "cpp": _c_error,
        "java": _java_error,
        "go": _go_error,
        "rust": _rust_error,
    }
    parser = parsers.get(lang, _python_error)
    return parser(stderr or "")


# ---------------------------------------------------------------------------
# 三级解决方案模板
# ---------------------------------------------------------------------------
PLACEHOLDER_MESSAGE = "{{message}}"
PLACEHOLDER_LINE = "{{line}}"

_PYTHON_TEMPLATES: Dict[str, Tuple[str, str, str]] = {
    "SyntaxError": (
        "检查第 {{line}} 行的冒号、括号配对、引号闭合，以及是否混入了中文标点。报错：{{message}}。",
        "把复杂表达式拆成多行、为嵌套条件加括号；提交前运行 `python -m py_compile 文件.py` 做语法门禁。",
        "拆分过长的函数与表达式，统一用 Black/Flake8 做格式化与静态检查，从流程上消灭语法错误。",
    ),
    "IndentationError": (
        "统一第 {{line}} 行所在代码块的缩进（建议 4 个空格），不要空格与 Tab 混用。报错：{{message}}。",
        "开启编辑器“显示空白字符”与自动缩进，提交前用 `python -m tabnanny 文件.py` 检查缩进一致性。",
        "用函数/类把深层嵌套摊平，减少超过 3 层的缩进结构，可读性提升的同时也减少缩进错误。",
    ),
    "NameError": (
        "第 {{line}} 行附近存在未定义或拼写错误的标识符，按报错“{{message}}”定位并改正名称。",
        "启用 pyflakes/mypy 做静态检查，并在函数开头显式初始化变量，避免依赖外部作用域的隐式名字。",
        "用数据类（dataclass）或字典封装相关状态，减少对松散全局变量的依赖，让名字来源一目了然。",
    ),
    "TypeError": (
        "第 {{line}} 行的操作用错了类型，例如对 None 做下标、字符串与整数相加。报错：{{message}}。",
        "在函数入口校验参数类型（isinstance 或类型注解 + mypy），对可能为 None 的返回值先判空再使用。",
        "统一接口的输入契约，用 dataclass / TypedDict 描述数据形状，避免“什么类型都往里传”的隐式约定。",
    ),
    "IndexError": (
        "第 {{line}} 行的下标越界；把循环上界从 `len(seq)` 改成正确边界，或先判断 `0 <= i < len(seq)`。报错：{{message}}。",
        "遍历元素优先用 `for item in seq`，需要索引时用 `enumerate(seq)`，从源头消除手工下标计算。",
        "把“取第 N 个元素”这类逻辑封装成带边界语义的函数，返回 Optional 或抛自定义异常，调用方必须处理空结果。",
    ),
    "ZeroDivisionError": (
        "第 {{line}} 行的分母可能为 0；先判断分母，为 0 时返回 0/None 或给出明确提示。报错：{{message}}。",
        "把所有除法集中到 `safe_div(a, b)` 之类的工具函数，内部统一处理 0 分母与非法输入。",
        "检查数据产生链路：为什么分母会是 0？在数据采集/校验阶段加上约束，而不是只在最后一刻兜底。",
    ),
    "KeyError": (
        "第 {{line}} 行访问了不存在的字典键；确认键名拼写，或改用 `dict.get(key, default)`。报错：{{message}}。",
        "对外部输入先做 schema 校验，访问前用 `if key in d` 判断，缺失时给出可诊断的默认值。",
        "用 dataclass/TypedDict 定义数据结构，让“有哪些键”成为代码契约，而不是散落在各处的字符串。",
    ),
    "AttributeError": (
        "第 {{line}} 行的对象没有该属性/方法，多半是对象为 None 或类名写错。报错：{{message}}。",
        "调用前判空，或使用 `getattr(obj, name, default)`；对可选依赖做显式能力检查。",
        "用协议（typing.Protocol）或抽象基类约束接口，让“对象应该有什么方法”在类型层面可检查。",
    ),
    "ValueError": (
        "第 {{line}} 行传入的值格式不对（如 int('abc')）；转换前先校验输入。报错：{{message}}。",
        "把用户输入解析集中到 parse_* 函数，捕获异常并转换为友好的提示，不要让原始异常冒泡到界面。",
        "在数据入口建立统一的校验层（pydantic 等），错误在数据边界就被拦住，业务代码只处理合法数据。",
    ),
    "FileNotFoundError": (
        "第 {{line}} 行打开的文件不存在；检查路径拼写，或用 `Path(...).exists()` 先判断。报错：{{message}}。",
        "用 `pathlib.Path` 构造路径并提供默认值，读取失败时记录完整路径与上下文，便于定位。",
        "把文件访问抽象为资源层，统一处理缺失、权限与编码问题，业务逻辑不直接依赖具体文件路径。",
    ),
    "ModuleNotFoundError": (
        "缺少依赖模块；按报错“{{message}}”用 `pip install <包名>` 安装，或修正 import 路径。",
        "在 requirements.txt 中锁定依赖版本，启动页提供依赖自检，避免运行到一半才报缺模块。",
        "按功能边界组织包结构，避免循环依赖与隐式 sys.path 操作，让模块归属清晰可维护。",
    ),
    "RecursionError": (
        "第 {{line}} 行递归没有正确终止条件或深度过大；检查 base case。报错：{{message}}。",
        "为递归函数加深度参数与上限保护，必要时改成显式栈的迭代实现。",
        "重新审视算法复杂度：用动态规划/尾递归改写，避免深层调用栈带来的崩溃风险。",
    ),
}

_GENERIC_TEMPLATES: Dict[str, Tuple[str, str, str]] = {
    "javascript": (
        "在第 {{line}} 行附近检查变量声明、类型与数组边界；报错：{{message}}。",
        "使用 `const/let` 明确作用域，访问数组前检查 `index < arr.length`，并用 try/catch 包住外部输入解析。",
        "拆分职责单一的小函数，开启 ESLint + TypeScript 类型检查，把隐式类型转换挡在运行之前。",
    ),
    "c": (
        "检查第 {{line}} 行的数组下标、指针与格式化字符串；报错：{{message}}。",
        "为所有数组访问加边界判断，检查 malloc/scanf 返回值，编译时使用 `-Wall -Wextra` 打开警告。",
        "把资源申请/释放封装成成对的函数，用静态分析（如 clang-tidy）检查越界与内存泄漏。",
    ),
    "cpp": (
        "检查第 {{line}} 行的迭代器/下标与容器容量；报错：{{message}}。",
        "优先使用 `at()`、`std::span` 等带边界检查的接口，RAII 管理资源，开启 `-Wall -Wextra`。",
        "用 STL 算法替代手写循环，明确所有权语义（unique_ptr/shared_ptr），减少裸指针带来的悬垂风险。",
    ),
    "java": (
        "检查第 {{line}} 行的空引用、数组边界与类型声明；报错：{{message}}。",
        "使用 Optional 表达可能为空的返回值，访问数组/集合前检查边界，编译开启 -Xlint 全部警告。",
        "按单一职责拆分大类，用明确的 DTO/VO 传递数据，配合 IDE 静态检查与单元测试覆盖异常分支。",
    ),
    "go": (
        "检查第 {{line}} 行的错误返回值与切片边界；报错：{{message}}。",
        "不要忽略 `err`，每次调用后立即判断；切片访问前校验长度，配合 `go vet` 做静态检查。",
        "把错误处理策略集中到边界层，用显式返回值代替 panic，并为关键路径补充表驱动测试。",
    ),
    "rust": (
        "阅读第 {{line}} 行的编译器提示，它通常已经给出修复建议；报错：{{message}}。",
        "用 `Option/Result` 显式表达失败可能，避免 unwrap；让编译器借用检查器帮你发现所有权问题。",
        "按领域模型组织模块，把错误类型归一到 thiserror/anyhow 定义的枚举，调用方必须显式处理失败分支。",
    ),
}


def _fill(template: str, info: ErrorInfo) -> str:
    return (
        template.replace(PLACEHOLDER_LINE, str(info.line or "未定位"))
        .replace(PLACEHOLDER_MESSAGE, info.message or "无详细消息")
        .strip()
    )


def build_solutions(info: ErrorInfo, language: str, ok: bool = False) -> List[Solution]:
    """永远返回三个方案。成功运行时给出质量改进建议，而不是空列表。"""
    lang = normalize_language(language)
    if ok:
        templates = (
            "当前代码已经能跑通；先确认第 {{line}} 行附近的边界输入（空值、空集合、极端值）是否被覆盖。",
            "补充 try/except 或语言等价的异常处理与输入校验，确保非法输入不会产生误导性结果。",
            "为这段代码补上单元测试并拆小函数，把“能跑”升级为“长期可维护”。",
        )
    elif lang == "python" and info.error_type in _PYTHON_TEMPLATES:
        templates = _PYTHON_TEMPLATES[info.error_type]
    else:
        templates = _GENERIC_TEMPLATES.get(
            lang,
            (
                "先定位第 {{line}} 行，按报错消息“{{message}}”修正最直接的语法/逻辑问题。",
                "加入输入校验与异常处理，保证同类错误在边界处被拦截而不是崩溃。",
                "拆分函数、明确接口契约并补单元测试，从结构上减少同类错误再次发生。",
            ),
        )
    levels = (
        ("方案一（最小改动）", "只修当前报错，改动范围最小"),
        ("方案二（防御性改写）", "让同类错误不再复发"),
        ("方案三（重构建议）", "从结构层面消除根因"),
    )
    return [
        Solution(level=level, title=title, detail=_fill(template, info))
        for (level, title), template in zip(levels, templates)
    ]


def diagnose_code(
    code: str,
    language: str = "python",
    timeout: int = 5,
    force_subprocess: bool = False,
    inputs: Optional[Sequence[Any]] = None,
    input_fallbacks: Optional[Sequence[Any]] = None,
    input_default: str = SAFE_INPUT_DEFAULT,
) -> DiagnosisResult:
    """诊断主流程：语法优先 -> 执行探测 -> 错误定位 -> 三级方案。"""
    lang = normalize_language(language)
    notes: List[str] = []

    if lang == "python":
        try:
            ast.parse(code)
        except SyntaxError as exc:
            info = error_from_syntax_error(exc, code)
            return DiagnosisResult(
                ok=False, language=lang, engine="ast", error=info,
                solutions=build_solutions(info, lang),
                stderr=str(exc), note="语法检查命中，未启动外部进程", syntax_checked=True,
            )
        notes.append("ast.parse 语法检查通过")
    else:
        notes.append("非 Python 语言，跳过 ast 语法检查，直接执行探测")

    execution: ExecutionResult = execute_code(
        code, lang, timeout=timeout, force_subprocess=force_subprocess,
        inputs=inputs, input_fallbacks=input_fallbacks, input_default=input_default,
    )
    if execution.note:
        notes.append(execution.note)

    if execution.success:
        info = ErrorInfo("无报错", None, "代码执行成功",
                         execution.stderr[:400])
        return DiagnosisResult(
            ok=True, language=lang, engine=execution.engine, error=info,
            solutions=build_solutions(info, lang, ok=True),
            stdout=execution.stdout, stderr=execution.stderr,
            returncode=execution.returncode, timed_out=execution.timed_out,
            note="；".join(notes), syntax_checked=lang == "python",
        )

    if execution.timed_out:
        info = ErrorInfo(
            "执行超时", None,
            f"代码运行超过 {timeout} 秒，已被强制终止。常见原因：死循环、阻塞输入、无限递归。",
            execution.stderr[:400],
        )
    else:
        info = parse_error(execution.stderr, lang)
    return DiagnosisResult(
        ok=False, language=lang, engine=execution.engine, error=info,
        solutions=build_solutions(info, lang),
        stdout=execution.stdout, stderr=execution.stderr,
        returncode=execution.returncode, timed_out=execution.timed_out,
        note="；".join(notes), syntax_checked=lang == "python",
    )


if __name__ == "__main__":  # 快速自测
    result = diagnose_code("print(1 / 0)", "python", force_subprocess=True)
    print(result.error)
    for solution in result.solutions:
        print(solution.level, "-", solution.detail[:50])
