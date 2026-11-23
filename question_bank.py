# -*- coding: utf-8 -*-
"""
question_bank.py —— 静态题库种子、AI 动态出题、Stack Overflow 官方 API 导入

题库来源三条路径：
    1. STATIC_QUESTIONS：内置题目，首次启动写入 questions.db，保证离线可用；
    2. OpenAI 兼容接口：API Key 只从界面输入或环境变量读取，绝不硬编码/入库；
    3. Stack Exchange API v2.3：官方接口，不做 HTML 爬虫。

合规说明（重要）：
    本模块从未对 stackoverflow.com 发起 HTML 爬取。官方 API 与网页的
    Acceptable Use Policy 不同，但为满足更严格的约束，这里仍先读取 robots.txt、
    解析 /questions/ 的 crawl-delay，并保证任意两次请求间隔不小于该值。
    如果 robots.txt 不可达，采用保守的 10 秒间隔；不达条件不发请求。
"""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from database import Database
from executor import execute_code, normalize_language, normalize_output

USER_AGENT = "CodeTutorDesktop/1.0 (educational local application)"
ROBOTS_URL = "https://stackoverflow.com/robots.txt"
API_ROOT = "https://api.stackexchange.com/2.3"

ProgressCallback = Optional[Callable[[str], None]]


def _notify(callback: ProgressCallback, message: str) -> None:
    if callback:
        callback(message)


# ---------------------------------------------------------------------------
# 静态题库：含错代码 + 期望输出 + 正确代码
# ---------------------------------------------------------------------------
STATIC_QUESTIONS: List[Dict[str, str]] = [
    {
        "language": "python",
        "title": "除零错误：安全分配",
        "description": "运行下面的代码会抛出 ZeroDivisionError。请修改代码，使 parts 为 0 时输出 0，其他情况下正常整除。",
        "buggy_code": "def split_count(items, parts):\n    return len(items) // parts\n\nprint(split_count([1, 2, 3], 0))\n",
        "expected_output": "0",
        "correct_code": "def split_count(items, parts):\n    if parts <= 0:\n        return 0\n    return len(items) // parts\n\nprint(split_count([1, 2, 3], 0))\n",
        "source": "static", "source_url": "",
    },
    {
        "language": "python",
        "title": "拼写错误：变量未定义",
        "description": "下面代码想输出 Hello, Alice，但变量名拼错了。请修正后提交。",
        "buggy_code": "def greet(name):\n    print('Hello, ' + nmae)\n\ngreet('Alice')\n",
        "expected_output": "Hello, Alice",
        "correct_code": "def greet(name):\n    print('Hello, ' + name)\n\ngreet('Alice')\n",
        "source": "static", "source_url": "",
    },
    {
        "language": "python",
        "title": "列表越界：循环上界",
        "description": "下面代码遍历列表时多走了一次，导致 IndexError。请修正循环上界，使输出为三行数字。",
        "buggy_code": "items = [10, 20, 30]\nfor i in range(4):\n    print(items[i])\n",
        "expected_output": "10\n20\n30",
        "correct_code": "items = [10, 20, 30]\nfor i in range(len(items)):\n    print(items[i])\n",
        "source": "static", "source_url": "",
    },
    {
        "language": "javascript",
        "title": "数组边界：循环多跑一次",
        "description": "下面代码因为循环条件写错，最终输出 NaN。请修正后提交，期望输出 6。",
        "buggy_code": "function sum(arr) {\n  let total = 0;\n  for (let i = 0; i <= arr.length; i++) {\n    total += arr[i];\n  }\n  return total;\n}\nconsole.log(sum([1, 2, 3]));\n",
        "expected_output": "6",
        "correct_code": "function sum(arr) {\n  let total = 0;\n  for (let i = 0; i < arr.length; i++) {\n    total += arr[i];\n  }\n  return total;\n}\nconsole.log(sum([1, 2, 3]));\n",
        "source": "static", "source_url": "",
    },
    {
        "language": "javascript",
        "title": "属性名拼写错误",
        "description": "下面代码访问了不存在的属性，抛出 TypeError。请修正属性名，使输出为 TOM。",
        "buggy_code": "const user = { name: 'Tom' };\nconsole.log(user.nmae.toUpperCase());\n",
        "expected_output": "TOM",
        "correct_code": "const user = { name: 'Tom' };\nconsole.log(user.name.toUpperCase());\n",
        "source": "static", "source_url": "",
    },
    {
        "language": "c",
        "title": "编译错误：变量未声明",
        "description": "下面代码无法通过编译，因为变量 n 未定义。请补充定义，使输出为 42。",
        "buggy_code": "#include <stdio.h>\nint main(void) {\n    printf(\"%d\\n\", n);\n    return 0;\n}\n",
        "expected_output": "42",
        "correct_code": "#include <stdio.h>\nint main(void) {\n    int n = 42;\n    printf(\"%d\\n\", n);\n    return 0;\n}\n",
        "source": "static", "source_url": "",
    },
    {
        "language": "c",
        "title": "算术异常：除数为 0",
        "description": "下面代码运行时会因为除数为 0 而异常终止。请加判断，使输出为 0。",
        "buggy_code": "#include <stdio.h>\nint main(void) {\n    int a = 5, b = 0;\n    printf(\"%d\\n\", a / b);\n    return 0;\n}\n",
        "expected_output": "0",
        "correct_code": "#include <stdio.h>\nint main(void) {\n    int a = 5, b = 0;\n    if (b == 0) {\n        printf(\"0\\n\");\n    } else {\n        printf(\"%d\\n\", a / b);\n    }\n    return 0;\n}\n",
        "source": "static", "source_url": "",
    },
    {
        "language": "java",
        "title": "数组越界：循环上界",
        "description": "下面代码抛出 ArrayIndexOutOfBoundsException。请修正循环上界，使输出为 6。",
        "buggy_code": "public class Main {\n    public static void main(String[] args) {\n        int[] nums = {1, 2, 3};\n        int sum = 0;\n        for (int i = 0; i <= nums.length; i++) {\n            sum += nums[i];\n        }\n        System.out.println(sum);\n    }\n}\n",
        "expected_output": "6",
        "correct_code": "public class Main {\n    public static void main(String[] args) {\n        int[] nums = {1, 2, 3};\n        int sum = 0;\n        for (int i = 0; i < nums.length; i++) {\n            sum += nums[i];\n        }\n        System.out.println(sum);\n    }\n}\n",
        "source": "static", "source_url": "",
    },
    {
        "language": "java",
        "title": "编译错误：变量名拼错",
        "description": "下面代码无法通过编译。请修正变量名，使输出为 Alice。",
        "buggy_code": "public class Main {\n    public static void main(String[] args) {\n        String name = \"Alice\";\n        System.out.println(nmae);\n    }\n}\n",
        "expected_output": "Alice",
        "correct_code": "public class Main {\n    public static void main(String[] args) {\n        String name = \"Alice\";\n        System.out.println(name);\n    }\n}\n",
        "source": "static", "source_url": "",
    },
]


def seed_static_questions(db: Database) -> int:
    """只在首次运行时播种；UNIQUE 索引保证重复调用不会产生重复题目。"""
    return db.add_questions(STATIC_QUESTIONS)


# ---------------------------------------------------------------------------
# 通用校验：用同一个执行引擎验证题库质量
# ---------------------------------------------------------------------------
def validate_question(question: Dict[str, Any]) -> Tuple[bool, str]:
    """
    题库质量门禁：正确代码必须能跑且输出等于期望值；含错代码不能刚好也通过。
    这里强制 force_subprocess=True，避免 coex 版本差异影响判题基准。
    """
    required = ("language", "title", "buggy_code", "expected_output", "correct_code")
    for key in required:
        if not isinstance(question.get(key), str) or not question[key].strip():
            return False, f"字段 {key} 缺失或为空"
    lang = normalize_language(question["language"])
    expected = normalize_output(question["expected_output"])
    good = execute_code(question["correct_code"], lang, timeout=5, force_subprocess=True)
    if not good.success:
        return False, f"正确代码运行失败：{good.stderr.strip()[:120] or '非零返回码'}"
    if normalize_output(good.stdout) != expected:
        return False, (
            f"正确代码输出与期望不符：{normalize_output(good.stdout)[:60]!r} "
            f"!= {expected[:60]!r}"
        )
    bad = execute_code(question["buggy_code"], lang, timeout=5, force_subprocess=True)
    if bad.success and normalize_output(bad.stdout) == expected:
        return False, "含错代码也能通过，题目没有区分度"
    return True, ""


# ---------------------------------------------------------------------------
# AI 动态出题（OpenAI 兼容接口）
# ---------------------------------------------------------------------------
def _extract_json_array(content: str) -> List[Dict[str, Any]]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("AI 返回内容中没有找到 JSON 数组")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("AI 返回的 JSON 顶层必须是数组")
    return data


def update_questions_from_ai(
    db: Database,
    language: str,
    api_key: str = "",
    api_base: str = "",
    model: str = "",
    count: int = 3,
    verify: bool = True,
    progress: ProgressCallback = None,
) -> Dict[str, Any]:
    """
    调用 OpenAI 兼容的 /chat/completions 生成题目。
    安全边界：
      - 密钥只来自参数或环境变量 OPENAI_API_KEY，只出现在本次请求的 Authorization 头；
        不写日志、不写数据库、不回显到界面（界面输入框为密码模式）。
      - 若绕过这一点把密钥硬编码进源码，任何拿到代码的人都能盗用配额。
    """
    key = (api_key or os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise ValueError("未配置 API Key：请在练习页填写，或设置环境变量 OPENAI_API_KEY")
    base = (api_base or os.getenv("OPENAI_API_BASE") or "https://api.openai.com/v1").rstrip("/")
    model_name = (model or os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    count = max(1, min(int(count or 3), 8))
    lang = normalize_language(language)

    _notify(progress, f"正在请求 AI 生成 {count} 道 {lang} 题目…")
    system_prompt = (
        "你是编程练习题出题人。只输出一个 JSON 数组，不要输出解释文字或 Markdown 代码块。"
        "数组每个元素必须包含字符串字段：title, description, buggy_code, "
        "expected_output, correct_code。buggy_code 是一段有典型错误的完整可运行程序，"
        "correct_code 是修复后的完整程序，expected_output 是 correct_code 的精确标准输出。"
        "题目难度为初学者水平，输出必须确定、不含时间戳或随机数。"
    )
    user_prompt = (
        f"请为 {lang} 语言生成 {count} 道“含错代码 + 期望输出”的修正练习题。"
        "确保 correct_code 运行后 stdout 严格等于 expected_output，"
        "而 buggy_code 会失败或输出不同。"
    )
    payload = json.dumps({
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        base + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {key}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        # 网络调用必须设超时，否则界面线程会永久挂起（这里在后台线程执行）
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            body = json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"AI 接口返回 HTTP {exc.code}：{detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"AI 接口调用失败：{type(exc).__name__}: {exc}") from exc

    try:
        content = body["choices"][0]["message"]["content"]
        raw_items = _extract_json_array(content)
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法解析 AI 返回内容：{exc}") from exc

    candidates: List[Dict[str, str]] = []
    for item in raw_items[:count]:
        if not isinstance(item, dict):
            continue
        candidates.append({
            "language": lang,
            "title": f"[AI] {str(item.get('title', 'AI 题目'))[:80]}",
            "description": str(item.get("description", ""))[:1000],
            "buggy_code": str(item.get("buggy_code", "")),
            "expected_output": str(item.get("expected_output", "")),
            "correct_code": str(item.get("correct_code", "")),
            "source": "ai", "source_url": "",
        })

    accepted: List[Dict[str, str]] = []
    skipped: List[str] = []
    for question in candidates:
        if verify:
            valid, reason = validate_question(question)
            if not valid:
                skipped.append(f"{question['title']}: {reason}")
                continue
        accepted.append(question)
    added = db.add_questions(accepted)
    db.add_source_log("ai", lang, added, "；".join(skipped)[:500])
    _notify(progress, f"AI 生成 {len(candidates)} 道，校验通过 {len(accepted)} 道，入库 {added} 道")
    return {
        "generated": len(candidates), "accepted": len(accepted),
        "added": added, "skipped": skipped, "model": model_name,
    }


# ---------------------------------------------------------------------------
# Stack Exchange 官方 API v2.3（不爬取 HTML）
# ---------------------------------------------------------------------------
def read_robots_delay(timeout: int = 10) -> float:
    """
    发任何请求前先读取 robots.txt 并解析 crawl-delay。
    若站点不可达则采用保守的 10 秒，宁可慢也不违反站点策略。
    """
    request = urllib.request.Request(ROBOTS_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except Exception:
        return 10.0
    blocks = re.split(r"\n\s*\n", text)
    global_delay: Optional[float] = None
    questions_delay: Optional[float] = None
    for block in blocks:
        lines = [line.strip() for line in block.splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        delay = None
        for line in lines:
            match = re.match(r"(?i)^crawl-delay\s*:\s*([\d.]+)", line)
            if match:
                try:
                    delay = float(match.group(1))
                except ValueError:
                    delay = None
        if any(re.match(r"(?i)^disallow\s*:\s*/questions", line) for line in lines):
            if delay is not None:
                questions_delay = delay
        elif delay is not None:
            global_delay = delay
    chosen = questions_delay if questions_delay is not None else global_delay
    return max(chosen or 10.0, 1.0)


def _api_get(endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{API_ROOT}/{endpoint}?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", errors="replace"))


def _extract_code_blocks(markup: str) -> List[str]:
    decoded = html.unescape(markup or "")
    blocks = re.findall(r"<pre[^>]*>\s*<code[^>]*>(.*?)</code>\s*</pre>",
                        decoded, flags=re.S | re.I)
    result: List[str] = []
    for block in blocks:
        text = html.unescape(re.sub(r"<[^>]+>", "", block)).strip()
        if 20 <= len(text) <= 4000:
            result.append(text)
    return result


LANGUAGE_TAGS = {
    "python": "python", "javascript": "javascript", "c": "c", "cpp": "c++",
    "java": "java", "go": "go", "rust": "rust",
}


def stack_exchange_update(
    db: Database,
    language: str,
    count: int = 2,
    progress: ProgressCallback = None,
) -> Dict[str, Any]:
    """
    通过官方 API 导入“含错代码 + 修复代码”题目，并标注来源与原问题链接。
    每次请求前 sleep(crawl-delay)，全程串行、无并发，符合站点策略。
    """
    delay = read_robots_delay()
    _notify(progress, f"已读取 robots.txt，请求间隔设为 {delay:.1f} 秒")
    lang = normalize_language(language)
    tag = LANGUAGE_TAGS.get(lang, lang)
    count = max(1, min(int(count or 2), 3))

    time.sleep(delay)  # 第一次请求也遵守间隔，避免连续探测
    questions_data = _api_get("questions", {
        "site": "stackoverflow", "tagged": tag,
        "filter": "withbody", "pagesize": min(count * 4, 15),
        "order": "desc", "sort": "votes",
    })
    items = questions_data.get("items", [])
    _notify(progress, f"官方 API 返回 {len(items)} 个候选问题")

    accepted: List[Dict[str, str]] = []
    skipped: List[str] = []
    for item in items:
        if len(accepted) >= count:
            break
        if not item.get("is_answered"):
            continue
        buggy_blocks = _extract_code_blocks(item.get("body", ""))
        if not buggy_blocks:
            continue
        buggy = buggy_blocks[0]
        time.sleep(delay)  # 串行请求，绝不并发
        try:
            answers = _api_get(f"questions/{item['question_id']}/answers", {
                "site": "stackoverflow", "filter": "withbody",
                "pagesize": 5, "order": "desc", "sort": "votes",
            })
        except Exception as exc:
            skipped.append(f"{item.get('title', '')[:40]}: 答案请求失败 {exc}")
            continue

        fixed = ""
        for answer in answers.get("items", []):
            for block in _extract_code_blocks(answer.get("body", "")):
                if block.strip() != buggy.strip():
                    fixed = block
                    break
            if fixed:
                break
        if not fixed:
            skipped.append(f"{item.get('title', '')[:40]}: 未找到可验证的修复代码")
            continue

        # 用修复代码的实际 stdout 作为期望输出，保证题目与执行引擎一致
        run = execute_code(fixed, lang, timeout=5, force_subprocess=True)
        if not run.success or not run.stdout.strip():
            skipped.append(f"{item.get('title', '')[:40]}: 修复代码无法产生确定输出")
            continue
        title = f"[SO] {str(item.get('title', 'Stack Overflow 问题'))[:70]}"
        question = {
            "language": lang, "title": title,
            "description": (
                "来源：Stack Overflow（官方 API v2.3）。以下问题代码存在缺陷，"
                "请参照原问题修复，使输出与期望值一致。"
            ),
            "buggy_code": buggy,
            "expected_output": normalize_output(run.stdout),
            "correct_code": fixed,
            "source": "stackoverflow",
            "source_url": str(item.get("link", "")),
        }
        valid, reason = validate_question(question)
        if not valid:
            skipped.append(f"{title[:40]}: {reason}")
            continue
        accepted.append(question)

    added = db.add_questions(accepted)
    db.add_source_log("stackoverflow", lang, added, "；".join(skipped)[:500])
    _notify(progress, f"Stack Overflow 导入完成：入库 {added} 道，跳过 {len(skipped)} 道")
    return {"added": added, "skipped": skipped, "delay": delay}


if __name__ == "__main__":  # 快速自测：python question_bank.py
    db = Database()
    print("静态题目数:", len(STATIC_QUESTIONS), "首次入库:", seed_static_questions(db))
    print("题库统计:", db.count_questions())
    valid, reason = validate_question(STATIC_QUESTIONS[0])
    print("样例题目校验:", valid, reason)
