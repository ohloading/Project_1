# -*- coding: utf-8 -*-
"""
executor.py —— 受限代码执行引擎（安全边界层）

设计目标
--------
1. 绝不调用 eval()/exec() 执行用户代码。用户代码只写入系统临时目录，
   然后交给一个独立的外部进程运行；主进程与用户代码完全隔离。
2. 优先使用 coex（支持多语言且自带超时/资源限制）；coex 未安装或调用
   失败时自动降级到 subprocess 执行器，并对语言做显式白名单限制。
3. 每一条执行路径都有：超时、输出长度上限、最小环境变量、独立工作目录、
   进程组清理。缺少这些边界时，用户代码可以死循环、吃满内存、破坏环境
   变量或留下子进程，因此任何绕过都可能让“练习平台”变成攻击入口。
4. 用户代码里的 input() 在非交互执行环境中拿不到标准输入，原本会直接
   EOFError 崩溃。这里改为：先读预定义测试数据（支持不同数据类型），
   数据不足时用安全默认值兜底，并用异常处理包住真实 input 调用。
   用户代码的其余逻辑、输出格式和预期结果完全不变。

依赖：
    pip install coex          # 可选；不装也能用（走 subprocess 降级路径）
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 安全常量：这些数值不要随意调大，输出上限用于防止内存被恶意输出撑爆
# ---------------------------------------------------------------------------
MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 5
COMPILE_TIMEOUT = 20
MAX_MEMORY_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024

# 显式语言白名单：降级执行器只认识这些语言，其他语言直接拒绝
LANGUAGE_PROFILES: Dict[str, Dict[str, Any]] = {
    "python": {"ext": ".py", "kind": "script"},
    "javascript": {"ext": ".js", "kind": "script"},
    "c": {"ext": ".c", "kind": "compile", "compiler": "gcc"},
    "cpp": {"ext": ".cpp", "kind": "compile", "compiler": "g++"},
    "java": {"ext": ".java", "kind": "java"},
    "go": {"ext": ".go", "kind": "go"},
    "rust": {"ext": ".rs", "kind": "compile", "compiler": "rustc"},
}

LANGUAGE_ALIASES = {
    "py": "python", "python3": "python",
    "js": "javascript", "node": "javascript", "nodejs": "javascript",
    "c++": "cpp", "cc": "cpp", "cxx": "cpp",
    "golang": "go", "rs": "rust",
    "文本文件": "python", "text": "python",
}

# 启动器：POSIX 下用它在 exec 之前设置资源限制。
# 之所以不用 subprocess 的 preexec_fn，是因为 GUI 程序是多线程的，
# preexec_fn 在多线程进程中可能导致死锁；用独立启动器更安全。
_LAUNCHER_SOURCE = '''\
# -*- coding: utf-8 -*-
import os, resource, sys
args = sys.argv[1:]
if args and args[0] == "--":
    args = args[1:]
try:
    cpu = int(os.environ.get("CT_CPU_LIMIT", "6"))
    mem = int(os.environ.get("CT_MEM_LIMIT", str(512 * 1024 * 1024)))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
except Exception:
    pass
if args:
    os.execvp(args[0], args)
'''

# Python 用户代码的 input() 安全适配层。
# 放在临时目录的 sitecustomize.py 中，由 Python 解释器在启动时自动加载，
# 因此不需要改写用户源码——行号、输出格式和原有逻辑都能保持不变。
# 安全边界：只在本进程的独立临时目录生效，不修改全局 Python 环境；
# 若绕过异常处理，无 stdin 时用户代码会因 EOFError 中断，诊断/判题结果失真。
_SAFE_INPUT_SOURCE = '''\
# -*- coding: utf-8 -*-
import builtins as _ct_builtins
import json as _ct_json
import os as _ct_os

_ct_real_input = _ct_builtins.input

def _ct_text(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)

def _ct_load_list(name):
    try:
        value = _ct_json.loads(_ct_os.environ.get(name, "[]"))
        return value if isinstance(value, list) else []
    except Exception:
        return []

# 不同数据类型的预定义测试输入：先由 stdin 管道提供，耗尽后由这里循环兜底
_ct_fallbacks = _ct_load_list("CT_INPUT_FALLBACKS")
_ct_default = _ct_os.environ.get("CT_INPUT_DEFAULT", "")
_ct_counter = [0]

def _ct_safe_input(prompt=""):
    try:
        # 交互环境/已提供 stdin 时保持原生行为：prompt 照常输出，数据照常读取
        return _ct_real_input(prompt)
    except (EOFError, OSError):
        # 非交互环境无可用标准输入：不抛异常，改用预定义数据或安全默认值。
        # 注意 prompt 已由原生 input 输出，因此界面显示与交互运行一致。
        index = _ct_counter[0]
        _ct_counter[0] += 1
        if _ct_fallbacks:
            return _ct_text(_ct_fallbacks[index % len(_ct_fallbacks)])
        return _ct_default

_ct_builtins.input = _ct_safe_input
'''

# 无预定义输入且未指定默认值时，input() 返回空字符串而不是抛 EOFError
SAFE_INPUT_DEFAULT = ""


@dataclass
class ExecutionResult:
    """一次执行的统一结果结构，coex 与 subprocess 两条路径都归一到这里。"""

    success: bool
    stdout: str
    stderr: str
    returncode: int
    language: str
    timed_out: bool = False
    engine: str = "subprocess"
    note: str = ""


def normalize_language(language: Optional[str]) -> str:
    """把用户可能的各种写法归一到白名单中的标准名称。"""
    if not language:
        return "python"
    key = str(language).strip().lower()
    key = LANGUAGE_ALIASES.get(key, key)
    return key if key in LANGUAGE_PROFILES else key


def detect_language(code: str, filename: Optional[str] = None) -> str:
    """上传文件后自动识别语言：先看扩展名，再看代码特征。"""
    if filename:
        ext = Path(filename).suffix.lower()
        ext_map = {
            ".py": "python", ".js": "javascript", ".mjs": "javascript",
            ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
            ".java": "java", ".go": "go", ".rs": "rust",
        }
        if ext in ext_map:
            return ext_map[ext]
    text = code or ""
    if re.search(r"^\s*(def\s+\w+|import\s+\w+|from\s+\w+\s+import\b)", text, re.M):
        return "python"
    if re.search(r"#include\s*[<\"\w]", text):
        return "cpp" if re.search(r"iostream|std::", text) else "c"
    if re.search(r"\bpublic\s+(?:final\s+)?class\s+\w+|\bSystem\.out\.print", text):
        return "java"
    if re.search(r"\bconsole\.log\s*\(|\bfunction\s+\w+\s*\(|\bconst\s+\w+\s*=", text):
        return "javascript"
    if re.search(r"^\s*func\s+main\s*\(|^\s*package\s+main\b", text, re.M):
        return "go"
    if re.search(r"^\s*fn\s+main\s*\(|^\s*use\s+std::", text, re.M):
        return "rust"
    return "python"


def supported_languages() -> str:
    return "、".join(LANGUAGE_PROFILES.keys())


def normalize_output(text: str) -> str:
    """判题用的输出归一：统一 CRLF/LF 并去除首尾空白，避免平台差异误判。"""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def input_value_to_text(value: Any) -> str:
    """把不同数据类型的测试输入转成 input() 会返回的字符串形式。"""
    if isinstance(value, str):
        return value
    if value is None:
        return SAFE_INPUT_DEFAULT
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def prepare_stdin(inputs: Optional[Sequence[Any]]) -> bytes:
    """把预定义测试输入写成 stdin 数据；空输入时返回空字节（关闭管道）。"""
    if not inputs:
        return b""
    lines = [input_value_to_text(value).replace("\r", "").replace("\n", " ")
             for value in inputs]
    return ("\n".join(lines) + "\n").encode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 子进程安全执行路径
# ---------------------------------------------------------------------------
def _minimal_env(
    workdir: Path,
    language: str,
    timeout: int,
    input_fallbacks: Optional[Sequence[Any]] = None,
    input_default: str = SAFE_INPUT_DEFAULT,
) -> Dict[str, str]:
    """只把必要环境变量传给子进程，避免继承 API Key 等敏感信息。"""
    env: Dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "TEMP": str(workdir),
        "TMP": str(workdir),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CT_CPU_LIMIT": str(max(1, timeout + 1)),
        "CT_MEM_LIMIT": str(MAX_MEMORY_BYTES),
        # input() 的安全回退数据：都是普通测试数据，不含任何密钥
        "CT_INPUT_FALLBACKS": json.dumps(list(input_fallbacks or []), ensure_ascii=False),
        "CT_INPUT_DEFAULT": input_default,
    }
    if platform.system() == "Windows":
        for key in ("SystemRoot", "ComSpec", "USERPROFILE"):
            if key in os.environ:
                env[key] = os.environ[key]
    if language == "python":
        # sitecustomize 在 Python 启动早期由 site 模块导入，此时 sys.path
        # 还没有脚本目录；把它所在的临时目录放进 PYTHONPATH 才能被找到。
        env["PYTHONPATH"] = str(workdir)
    if language == "go":
        # Go 首次编译会在缓存目录写大量文件，必须限制在临时目录内
        env["GO111MODULE"] = "off"
        env["GOPATH"] = str(workdir / "gopath")
        env["GOCACHE"] = str(workdir / "gocache")
        (workdir / "gopath").mkdir(exist_ok=True)
        (workdir / "gocache").mkdir(exist_ok=True)
    return env


def _decode(data: bytes) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """超时后杀掉整个进程组，防止用户代码派生的子进程成为孤儿进程。"""
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run(
    cmd: list, cwd: Path, env: Dict[str, str], timeout: int, launcher: Optional[Path],
    stdin_payload: bytes = b"",
) -> Tuple[str, str, int, bool]:
    """真正启动进程的位置：shell=False + 超时 + 输出上限，缺一不可。"""
    actual_cmd = [sys.executable, str(launcher), "--", *cmd] if launcher else cmd
    kwargs: Dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        # 用 PIPE 而不是 DEVNULL：预定义测试输入可通过 stdin 正常送达代码里的
        # input()。数据写完后管道立即关闭，不会产生可交互等待或挂起。
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,  # 绝对不能用 shell=True，否则分号/管道可执行任意系统命令
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # 便于超时时 killpg 清理整组

    proc = subprocess.Popen(actual_cmd, **kwargs)
    timed_out = False
    try:
        out_b, err_b = proc.communicate(input=stdin_payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
        try:
            # 超时后不再重复写入 stdin，只把残留输出读干净
            out_b, err_b = proc.communicate(timeout=3)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            out_b, err_b = b"", b""
    code = proc.returncode if proc.returncode is not None else -9
    return _decode(out_b), _decode(err_b), int(code), timed_out


def _source_filename(language: str, code: str) -> str:
    """Java 要求 public class 名与文件名一致，这里做一次保守适配。"""
    if language == "java":
        match = re.search(r"\b(?:public\s+)?(?:final\s+)?class\s+([A-Za-z_]\w*)", code)
        if match:
            return f"{match.group(1)}.java"
        return "Main.java"
    return "source" + LANGUAGE_PROFILES[language]["ext"]


def _run_subprocess(
    code: str,
    language: str,
    timeout: int,
    inputs: Optional[Sequence[Any]] = None,
    input_fallbacks: Optional[Sequence[Any]] = None,
    input_default: str = SAFE_INPUT_DEFAULT,
) -> ExecutionResult:
    lang = normalize_language(language)
    profile = LANGUAGE_PROFILES.get(lang)
    if profile is None:
        return ExecutionResult(
            False, "", f"不允许的语言：{language}。白名单：{supported_languages()}",
            -1, language, note="语言白名单拦截",
        )

    tool = profile.get("compiler") or ("python" if lang == "python" else None)
    if lang == "python":
        tool = sys.executable
    if tool and shutil.which(tool) is None and not (lang == "python" and Path(tool).exists()):
        return ExecutionResult(
            False, "", f"未检测到运行时/编译器：{tool}。请安装后重试。",
            -1, language, note="运行时缺失",
        )
    if lang == "javascript" and shutil.which("node") is None:
        return ExecutionResult(False, "", "未检测到 Node.js（node 命令）。", -1, language)
    if lang == "java" and (shutil.which("javac") is None or shutil.which("java") is None):
        return ExecutionResult(False, "", "未检测到 JDK（javac / java 命令）。", -1, language)

    stdin_payload = prepare_stdin(inputs)
    with tempfile.TemporaryDirectory(prefix="codetutor_") as tmp:
        workdir = Path(tmp)
        env = _minimal_env(workdir, lang, timeout, input_fallbacks, input_default)
        launcher: Optional[Path] = None
        if platform.system() != "Windows":
            # 安全边界：POSIX 上先设置 CPU/内存/文件大小限制再 exec 用户程序。
            # 若绕过这一步，死循环或内存炸弹会直接拖垮桌面应用所在主机。
            launcher = workdir / "_ct_launcher.py"
            launcher.write_text(_LAUNCHER_SOURCE, encoding="utf-8")

        src = workdir / _source_filename(lang, code)
        src.write_text(code, encoding="utf-8", errors="replace")
        exe_name = "program.exe" if platform.system() == "Windows" else "program"
        if lang == "python":
            # 只在本次执行的临时目录注入 input() 安全适配，不改写用户源码，
            # 因此错误行号、输出格式和预期结果都保持原样。
            (workdir / "sitecustomize.py").write_text(
                _SAFE_INPUT_SOURCE, encoding="utf-8"
            )

        if profile["kind"] == "script":
            cmd = [sys.executable, src.name] if lang == "python" else ["node", src.name]
            out, err, rc, timed_out = _run(
                cmd, workdir, env, timeout, launcher, stdin_payload
            )
        elif profile["kind"] == "compile":
            compiler = profile["compiler"]
            if lang == "rust":
                compile_cmd = [compiler, src.name, "-o", exe_name]
            else:
                compile_cmd = [compiler, src.name, "-o", exe_name]
            cout, cerr, crc, cto = _run(
                compile_cmd, workdir, env, COMPILE_TIMEOUT, launcher
            )
            if cto:
                return ExecutionResult(False, cout, "编译超时。", -1, lang, True, "subprocess")
            if crc != 0:
                return ExecutionResult(False, cout, cerr, crc, lang, False, "subprocess",
                                       "编译失败，尚未运行用户代码")
            out, err, rc, timed_out = _run(
                [str(workdir / exe_name)], workdir, env, timeout, launcher, stdin_payload
            )
        elif profile["kind"] == "java":
            cout, cerr, crc, cto = _run(
                ["javac", src.name], workdir, env, COMPILE_TIMEOUT, launcher
            )
            if cto:
                return ExecutionResult(False, cout, "Java 编译超时。", -1, lang, True, "subprocess")
            if crc != 0:
                return ExecutionResult(False, cout, cerr, crc, lang, False, "subprocess",
                                       "编译失败，尚未运行用户代码")
            class_name = src.stem
            out, err, rc, timed_out = _run(
                ["java", "-cp", ".", class_name], workdir, env, timeout, launcher,
                stdin_payload,
            )
        elif profile["kind"] == "go":
            out, err, rc, timed_out = _run(
                ["go", "run", src.name], workdir, env, COMPILE_TIMEOUT, launcher,
                stdin_payload,
            )
        else:  # pragma: no cover - 白名单保证不会走到这里
            return ExecutionResult(False, "", "未知执行类型。", -1, lang, note="执行类型错误")

    return ExecutionResult(
        success=(rc == 0 and not timed_out),
        stdout=out,
        stderr=err,
        returncode=rc,
        language=lang,
        timed_out=timed_out,
        engine="subprocess",
    )


# ---------------------------------------------------------------------------
# coex 优先路径（不同 coex 版本 API 有差异，因此做防御式适配）
# ---------------------------------------------------------------------------
def _field(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
    else:
        for name in names:
            if hasattr(obj, name):
                return getattr(obj, name)
    return default


def _normalize_coex_result(result: Any, lang: str) -> Optional[ExecutionResult]:
    stdout = _field(result, "stdout", "output", "out")
    stderr = _field(result, "stderr", "error", "err", "errors")
    if stdout is None and stderr is None:
        return None
    rc = _field(result, "returncode", "exit_code", "code", "status")
    ok = _field(result, "success", "ok", "passed")
    if stderr is None:
        stderr = ""
    if stdout is None:
        stdout = ""
    if isinstance(stderr, (dict, list)):
        stderr = json.dumps(stderr, ensure_ascii=False)
    if not isinstance(stdout, str):
        stdout = str(stdout)
    if not isinstance(stderr, str):
        stderr = str(stderr)
    if ok is None:
        ok = (rc == 0) if rc is not None else (not bool(stderr.strip()))
    try:
        rc_value = int(rc) if rc is not None else (0 if ok else -1)
    except (TypeError, ValueError):
        rc_value = 0 if ok else -1
    return ExecutionResult(
        success=bool(ok), stdout=stdout[:MAX_OUTPUT_CHARS],
        stderr=stderr[:MAX_OUTPUT_CHARS], returncode=rc_value,
        language=lang, engine="coex",
        timed_out=bool(_field(result, "timed_out", "timeout", default=False)),
    )


def _try_coex(code: str, language: str, timeout: int) -> Optional[ExecutionResult]:
    """coex 是可选依赖：只要它不可用或返回格式无法识别，就静默降级。"""
    try:
        import coex  # type: ignore
    except Exception:
        return None
    lang = normalize_language(language)
    candidates: list[Callable[..., Any]] = []
    for attr in ("run", "execute", "exec"):
        fn = getattr(coex, attr, None)
        if callable(fn):
            candidates.append(fn)
    factory = getattr(coex, "CodeExecutor", None) or getattr(coex, "Executor", None)
    if factory:
        try:
            instance = factory()
            for attr in ("run", "execute", "exec"):
                fn = getattr(instance, attr, None)
                if callable(fn):
                    candidates.append(fn)
        except Exception:
            pass

    # 只用关键字参数调用：不同 coex 版本的位置参数语义不同（例如某些版本
    # 第一个位置是 inputs 而不是 code），位置调用可能把代码当成测试输入，
    # 造成难以排查的错误。拿不到 stdout/stderr 的返回值一律视为不可用。
    attempts = []
    for fn in candidates:
        attempts.append(lambda fn=fn: fn(code=code, language=lang, timeout=timeout))
        attempts.append(lambda fn=fn: fn(code=code, language=lang, time_limit=timeout))
    for call in attempts:
        try:
            result = call()
        except TypeError:
            continue
        except Exception:
            return None
        normalized = _normalize_coex_result(result, lang)
        if normalized is not None:
            return normalized
    return None


def execute_code(
    code: str,
    language: str = "python",
    timeout: int = DEFAULT_TIMEOUT,
    prefer_coex: bool = True,
    force_subprocess: bool = False,
    inputs: Optional[Sequence[Any]] = None,
    input_fallbacks: Optional[Sequence[Any]] = None,
    input_default: str = SAFE_INPUT_DEFAULT,
) -> ExecutionResult:
    """对外统一入口：先 coex，后 subprocess；两条路径都有完整安全边界。"""
    lang = normalize_language(language)
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), 60))
    if prefer_coex and not force_subprocess:
        result = _try_coex(code, lang, timeout)
        if result is not None:
            return result
    result = _run_subprocess(
        code, lang, timeout,
        inputs=inputs, input_fallbacks=input_fallbacks, input_default=input_default,
    )
    if prefer_coex and not force_subprocess and not result.note:
        result.note = "coex 不可用或调用失败，已降级为 subprocess 执行器"
    return result


if __name__ == "__main__":  # 简单自测：python executor.py
    demo = "print('executor ok')\n"
    r = execute_code(demo, "python", force_subprocess=True)
    print(r)
