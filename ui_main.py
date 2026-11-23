# -*- coding: utf-8 -*-
"""
ui_main.py —— PyQt5 主界面（诊断页 + 练习页）

编辑器优先使用 QScintilla（原生语法高亮 + setAutoIndent(True) 自动缩进）。
若本机未安装 QScintilla，则降级为 QPlainTextEdit 并在状态栏提示安装命令；
业务功能不受影响，但强烈建议按 README 安装 QScintilla 以获得完整体验。

所有耗时操作（执行用户代码、AI 请求、Stack Overflow API）都放进 QThread，
避免阻塞 Qt 事件循环导致界面假死。
"""

from __future__ import annotations

import json
import os
from html import escape as html_escape
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from database import Database
from diagnostics import diagnose_code
from executor import (
    DEFAULT_TIMEOUT, SAFE_INPUT_DEFAULT, detect_language, execute_code,
    normalize_language, normalize_output, supported_languages,
)
from question_bank import (
    seed_static_questions, stack_exchange_update, update_questions_from_ai,
)

try:
    from PyQt5.Qsci import (
        QsciLexerBash, QsciLexerCPP, QsciLexerJava, QsciLexerJavaScript,
        QsciLexerPython, QsciScintilla,
    )
    QSCI_AVAILABLE = True
    QSCI_ERROR = ""
except Exception as exc:  # pragma: no cover - 取决于本机是否安装 QScintilla
    QSCI_AVAILABLE = False
    QSCI_ERROR = str(exc)


LANGUAGE_ITEMS = ["python", "javascript", "c", "cpp", "java", "go", "rust"]


# ---------------------------------------------------------------------------
# 代码编辑控件：QScintilla 优先，QPlainTextEdit 仅作可用性兜底
# ---------------------------------------------------------------------------
if QSCI_AVAILABLE:
    class CodeEditor(QsciScintilla):
        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setAutoIndent(True)  # 需求约束：原生自动缩进
            self.setTabWidth(4)
            self.setIndentationsUseTabs(False)
            self.setIndentationGuides(True)
            self.setBraceMatching(getattr(QsciScintilla, "SloppyBraceMatch", 1))
            self.setCaretLineVisible(True)
            self.setCaretLineBackgroundColor(QColor("#eef4ff"))
            self.setMarginLineNumbers(0, True)
            self.setMarginWidth(0, "0000")
            self.setFont(QFont("Consolas", 10))
            self.setLanguage("python")

        def setLanguage(self, language: str) -> None:
            lang = normalize_language(language)
            mapping = {
                "python": QsciLexerPython,
                "javascript": QsciLexerJavaScript,
                "c": QsciLexerCPP,
                "cpp": QsciLexerCPP,
                "java": QsciLexerJava,
                "go": QsciLexerBash,
                "rust": QsciLexerBash,
            }
            lexer_class = mapping.get(lang)
            if lexer_class is None:
                return
            lexer = lexer_class(self)
            lexer.setFont(QFont("Consolas", 10))
            self.setLexer(lexer)
else:
    class CodeEditor(QPlainTextEdit):  # type: ignore[no-redef]
        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setFont(QFont("Consolas", 10))
            self.setTabStopDistance(4 * self.fontMetrics().width("M"))
            self.setStyleSheet(
                "QPlainTextEdit { background: #ffffff; border: 1px solid #d6dee9; }"
            )

        def setLanguage(self, language: str) -> None:
            pass

        def text(self) -> str:
            return self.toPlainText()

        def setText(self, value: str) -> None:
            self.setPlainText(value)


# ---------------------------------------------------------------------------
# 后台 worker
# ---------------------------------------------------------------------------
class FunctionWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._func = func
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            if "progress" in self._kwargs:
                self._kwargs["progress"] = self.progress.emit
            self.completed.emit(self._func(*self._args, **self._kwargs))
        except Exception as exc:  # 异常跨线程不能直接弹窗，用信号回主线程处理
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def parse_test_inputs(text: str) -> List[Any]:
    """
    把界面上的“测试输入”解析成不同数据类型：用 | 分隔，
    支持整数、浮点数、布尔/空值与普通字符串；留空则不提供输入。
    """
    if not text or not text.strip():
        return []
    values: List[Any] = []
    for chunk in text.split("|"):
        token = chunk.strip()
        if not token:
            continue
        lowered = token.lower()
        if lowered in ("true", "false"):
            values.append(lowered == "true")
        elif lowered in ("none", "null"):
            values.append(None)
        else:
            try:
                values.append(int(token))
                continue
            except ValueError:
                pass
            try:
                values.append(float(token))
                continue
            except ValueError:
                pass
            values.append(token)
    return values


def judge_answer(
    code: str,
    language: str,
    expected_output: str,
    timeout: int = DEFAULT_TIMEOUT,
    inputs: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """
    判题必须基于真实执行输出，不能做静态文本匹配。
    这里强制 subprocess 路径，保证与题库校验使用同一套判定基准。
    测试输入既可作 stdin 数据，也作 EOF 后的循环回退值，避免 input() 崩溃。
    """
    execution = execute_code(
        code, language, timeout=timeout, force_subprocess=True,
        inputs=inputs, input_fallbacks=inputs, input_default=SAFE_INPUT_DEFAULT,
    )
    actual = normalize_output(execution.stdout)
    expected = normalize_output(expected_output)
    return {
        "passed": bool(execution.success and actual == expected),
        "actual": actual,
        "expected": expected,
        "stderr": execution.stderr,
        "timed_out": execution.timed_out,
        "returncode": execution.returncode,
        "engine": execution.engine,
    }


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _table_item(value: Any) -> QTableWidgetItem:
    item = QTableWidgetItem(str(value if value is not None else ""))
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


def _pre(text: str) -> str:
    return f"<pre style='white-space:pre-wrap;'>{html_escape(text or '')}</pre>"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("多语言代码诊断与修正练习平台")
        self.resize(1180, 780)
        self.db = Database()
        seed_static_questions(self.db)
        self._workers: List[QThread] = []
        self.questions: List[Dict[str, Any]] = []
        self.current_question: Optional[Dict[str, Any]] = None
        self.current_index = 0

        self._build_ui()
        self._refresh_history_types()
        self.statusBar().showMessage(
            "编辑器：QScintilla（已启用自动缩进）" if QSCI_AVAILABLE
            else f"编辑器：QPlainTextEdit 降级模式（pip install QScintilla） - {QSCI_ERROR[:80]}"
        )
        self.load_questions(silent=True)

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        tabs = QTabWidget()
        tabs.addTab(self._build_diagnosis_tab(), "代码诊断")
        tabs.addTab(self._build_practice_tab(), "修正练习")
        self.setCentralWidget(tabs)

    def _build_diagnosis_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        toolbar = QHBoxLayout()
        self.btn_upload = QPushButton("上传代码 / TXT 文件")
        self.btn_upload.clicked.connect(self.upload_file)
        self.combo_diag_lang = QComboBox()
        self.combo_diag_lang.addItem("自动识别")
        self.combo_diag_lang.addItems(LANGUAGE_ITEMS)
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(1, 30)
        self.spin_timeout.setValue(DEFAULT_TIMEOUT)
        self.spin_timeout.setToolTip("用户代码最长运行秒数")
        self.edit_diag_inputs = QLineEdit()
        self.edit_diag_inputs.setPlaceholderText("测试输入(可选)，用 | 分隔，如 5|Tom|3.5")
        self.edit_diag_inputs.setToolTip(
            "预定义 input() 数据：支持整数/浮点/布尔/字符串；留空时无输入则使用安全默认值"
        )
        self.edit_diag_inputs.setFixedWidth(230)
        self.btn_diagnose = QPushButton("开始诊断")
        self.btn_diagnose.clicked.connect(self.start_diagnosis)
        self.btn_diag_history = QPushButton("历史样例")
        self.btn_diag_history.clicked.connect(self.show_diagnosis_history)
        self.lbl_file = QLabel("尚未上传文件（也可直接在编辑框粘贴代码）")
        toolbar.addWidget(self.btn_upload)
        toolbar.addWidget(QLabel("语言："))
        toolbar.addWidget(self.combo_diag_lang)
        toolbar.addWidget(QLabel("超时(秒)："))
        toolbar.addWidget(self.spin_timeout)
        toolbar.addWidget(self.edit_diag_inputs)
        toolbar.addWidget(self.btn_diagnose)
        toolbar.addWidget(self.btn_diag_history)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        layout.addWidget(self.lbl_file)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("代码预览 / 编辑区："))
        self.diag_editor = CodeEditor()
        left_layout.addWidget(self.diag_editor)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("诊断结果与分级方案："))
        self.diag_result = QTextBrowser()
        self.diag_result.setOpenExternalLinks(True)
        self.diag_result.setHtml(
            "<h3>等待诊断</h3><p>上传文件或粘贴代码后点击“开始诊断”。</p>"
            "<p>支持语言：" + html_escape(supported_languages()) + "</p>"
        )
        right_layout.addWidget(self.diag_result)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)
        return page

    def _build_practice_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        top = QHBoxLayout()
        top.addWidget(QLabel("练习语言："))
        self.combo_practice_lang = QComboBox()
        self.combo_practice_lang.addItems(LANGUAGE_ITEMS)
        self.combo_practice_lang.currentTextChanged.connect(lambda _: self.load_questions(True))
        top.addWidget(self.combo_practice_lang)
        self.btn_load_questions = QPushButton("加载题目")
        self.btn_load_questions.clicked.connect(lambda: self.load_questions(False))
        top.addWidget(self.btn_load_questions)
        self.btn_prev = QPushButton("上一题")
        self.btn_prev.clicked.connect(self.previous_question)
        top.addWidget(self.btn_prev)
        self.btn_next = QPushButton("下一题")
        self.btn_next.clicked.connect(self.next_question)
        top.addWidget(self.btn_next)
        self.lbl_progress = QLabel("尚未加载题目")
        top.addWidget(self.lbl_progress)
        self.edit_practice_inputs = QLineEdit()
        self.edit_practice_inputs.setPlaceholderText("测试输入(可选)，用 | 分隔")
        self.edit_practice_inputs.setToolTip(
            "为代码中的 input() 预定义数据；留空时无输入将使用安全默认值，不会 EOFError"
        )
        self.edit_practice_inputs.setFixedWidth(200)
        top.addWidget(self.edit_practice_inputs)
        top.addStretch(1)
        self.btn_practice_history = QPushButton("作答历史")
        self.btn_practice_history.clicked.connect(self.show_practice_history)
        top.addWidget(self.btn_practice_history)
        layout.addLayout(top)

        update_box = QGroupBox("题库更新（可选）：AI 兼容接口 / Stack Exchange 官方 API")
        update_layout = QHBoxLayout(update_box)
        form = QFormLayout()
        self.edit_api_base = QLineEdit(os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"))
        self.edit_model = QLineEdit(os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
        self.edit_api_key = QLineEdit(os.getenv("OPENAI_API_KEY", ""))
        self.edit_api_key.setEchoMode(QLineEdit.Password)  # 密钥不明文显示
        self.edit_api_key.setPlaceholderText("留空则使用环境变量 OPENAI_API_KEY")
        self.spin_count = QSpinBox()
        self.spin_count.setRange(1, 8)
        self.spin_count.setValue(3)
        form.addRow("API Base：", self.edit_api_base)
        form.addRow("模型：", self.edit_model)
        form.addRow("API Key：", self.edit_api_key)
        form.addRow("生成数量：", self.spin_count)
        update_layout.addLayout(form)
        side = QVBoxLayout()
        self.btn_ai_update = QPushButton("开启 AI 更新题库")
        self.btn_ai_update.clicked.connect(self.ai_update)
        self.btn_stack_update = QPushButton("Stack Overflow 官方 API 导入")
        self.btn_stack_update.clicked.connect(self.stack_update)
        self.lbl_source_note = QLabel(
            "API Key 只保存在内存，不写数据库；Stack Overflow 题目会标注来源和原链接。"
        )
        self.lbl_source_note.setWordWrap(True)
        side.addWidget(self.btn_ai_update)
        side.addWidget(self.btn_stack_update)
        side.addWidget(self.lbl_source_note)
        side.addStretch(1)
        update_layout.addLayout(side)
        layout.addWidget(update_box)

        self.lbl_question_title = QLabel("请先选择语言并加载题目")
        self.lbl_question_title.setStyleSheet("font-weight: bold; font-size: 12pt;")
        layout.addWidget(self.lbl_question_title)
        self.lbl_description = QLabel("")
        self.lbl_description.setWordWrap(True)
        self.lbl_description.setOpenExternalLinks(True)
        layout.addWidget(self.lbl_description)
        self.lbl_expected = QLabel("")
        self.lbl_expected.setWordWrap(True)
        layout.addWidget(self.lbl_expected)

        layout.addWidget(QLabel("请在下方修改代码后提交："))
        self.answer_editor = CodeEditor()
        layout.addWidget(self.answer_editor)
        self.btn_submit = QPushButton("提交作答")
        self.btn_submit.clicked.connect(self.submit_answer)
        layout.addWidget(self.btn_submit, alignment=Qt.AlignLeft)

        self.feedback = QTextBrowser()
        self.feedback.setMaximumHeight(160)
        self.feedback.setHtml("<p>判题结果将显示在这里。</p>")
        layout.addWidget(self.feedback)

        self.lbl_correct = QLabel("正确代码（参考）：")
        self.lbl_correct.setVisible(False)
        layout.addWidget(self.lbl_correct)
        self.correct_view = QTextBrowser()
        self.correct_view.setMaximumHeight(180)
        self.correct_view.setVisible(False)
        layout.addWidget(self.correct_view)
        return page

    # ------------------------------------------------------------------
    # 后台任务封装
    # ------------------------------------------------------------------
    def _run_worker(
        self, func: Callable[..., Any], on_done: Callable[[Any], None],
        *args: Any, on_error: Optional[Callable[[str], None]] = None, **kwargs: Any
    ) -> FunctionWorker:
        worker = FunctionWorker(func, *args, **kwargs)
        worker.completed.connect(on_done)
        worker.failed.connect(on_error or self._default_worker_error)
        worker.progress.connect(self.statusBar().showMessage)
        self._workers.append(worker)
        worker.finished.connect(lambda w=worker: self._workers.remove(w) if w in self._workers else None)
        worker.start()
        return worker

    def _default_worker_error(self, message: str) -> None:
        QMessageBox.warning(self, "操作失败", message)
        self.btn_diagnose.setEnabled(True)
        self.btn_submit.setEnabled(True)
        self.btn_ai_update.setEnabled(True)
        self.btn_stack_update.setEnabled(True)

    # ------------------------------------------------------------------
    # 模块 A：诊断
    # ------------------------------------------------------------------
    def upload_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择代码文件或 TXT", "",
            "代码/文本 (*.py *.c *.cpp *.cc *.h *.java *.js *.go *.rs *.txt);;所有文件 (*)",
        )
        if not path:
            return
        try:
            code = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            QMessageBox.warning(self, "读取失败", str(exc))
            return
        language = detect_language(code, path)
        self.diag_editor.setText(code)
        index = self.combo_diag_lang.findText(language)
        if index >= 0:
            self.combo_diag_lang.setCurrentIndex(index)
        self.lbl_file.setText(f"已加载：{path}（自动识别语言：{language}）")

    def start_diagnosis(self) -> None:
        code = self.diag_editor.text()
        if not code.strip():
            QMessageBox.information(self, "提示", "请先上传文件或粘贴代码。")
            return
        choice = self.combo_diag_lang.currentText()
        language = detect_language(code) if choice == "自动识别" else normalize_language(choice)
        timeout = self.spin_timeout.value()
        test_inputs = parse_test_inputs(self.edit_diag_inputs.text())
        self.btn_diagnose.setEnabled(False)
        self.diag_result.setHtml("<p>诊断中…（用户代码在独立进程中受限运行）</p>")
        self._run_worker(
            diagnose_code, self._on_diagnosis_done,
            code, language, timeout,
            inputs=test_inputs, input_fallbacks=test_inputs,
            input_default=SAFE_INPUT_DEFAULT,
            on_error=self._default_worker_error,
        )

    def _on_diagnosis_done(self, result: Any) -> None:
        self.btn_diagnose.setEnabled(True)
        parts: List[str] = ["<h3>诊断结果</h3>"]
        info = result.error
        parts.append(
            "<table border='0' cellpadding='3'>"
            f"<tr><td><b>运行状态：</b></td><td>{'执行成功' if result.ok else '发现错误'}</td></tr>"
            f"<tr><td><b>语言 / 引擎：</b></td><td>{html_escape(result.language)} / {html_escape(result.engine)}</td></tr>"
            f"<tr><td><b>错误类型：</b></td><td>{html_escape(info.error_type if info else '-')}</td></tr>"
            f"<tr><td><b>错误行号：</b></td><td>{info.line if info and info.line else '未定位'}</td></tr>"
            f"<tr><td><b>错误消息：</b></td><td>{html_escape(info.message if info else '')}</td></tr>"
            "</table>"
        )
        if result.note:
            parts.append(f"<p><i>{html_escape(result.note)}</i></p>")
        if result.stderr.strip():
            parts.append("<h4>stderr：</h4>" + _pre(result.stderr[:2000]))
        if result.stdout.strip():
            parts.append("<h4>stdout：</h4>" + _pre(result.stdout[:2000]))
        parts.append("<h3>分级解决方案</h3>")
        for solution in result.solutions:
            parts.append(
                f"<h4 style='color:#2f6fed;'>{html_escape(solution.level)}："
                f"{html_escape(solution.title)}</h4>"
                f"<p>{html_escape(solution.detail)}</p>"
            )
        self.diag_result.setHtml("".join(parts))

        # 数据库写入：错误类型作为分类键，方案序列化为 JSON 文本。
        # 若不持久化，用户就无法按错误类型回看历史样例，学习闭环会断掉。
        self.db.add_diagnosis(
            error_type=info.error_type if info else "未知错误",
            language=result.language,
            error_line=info.line if info else None,
            error_message=info.message if info else "",
            code=self.diag_editor.text(),
            solutions=[s.as_dict() for s in result.solutions],
            engine=result.engine,
        )
        self._refresh_history_types()

    def _refresh_history_types(self) -> None:
        self._history_types = self.db.diagnosis_types()

    def show_diagnosis_history(self) -> None:
        # 打开面板时重新读取错误类型，避免筛选下拉与库内数据不同步
        self._refresh_history_types()
        dialog = QDialog(self)
        dialog.setWindowTitle("历史样例（按报错类型筛选）")
        dialog.resize(980, 660)
        layout = QVBoxLayout(dialog)
        combo = QComboBox()
        combo.addItem("全部类型")
        combo.addItems(getattr(self, "_history_types", []))
        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["ID", "时间", "语言", "错误类型", "行号", "错误消息"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)

        delete_bar = QHBoxLayout()
        edit_delete_id = QLineEdit()
        edit_delete_id.setPlaceholderText("输入样例 ID；留空则删除上方选中的行")
        edit_delete_id.setFixedWidth(260)
        btn_delete = QPushButton("删除该历史样例")
        delete_bar.addWidget(QLabel("样例 ID："))
        delete_bar.addWidget(edit_delete_id)
        delete_bar.addWidget(btn_delete)
        delete_bar.addStretch(1)

        detail = QTextBrowser()
        layout.addWidget(combo)
        layout.addWidget(table)
        layout.addLayout(delete_bar)
        layout.addWidget(QLabel("详情（代码 + 当时的三级方案）："))
        layout.addWidget(detail)

        def reload() -> None:
            error_type = combo.currentText()
            if error_type == "全部类型":
                records = self.db.get_all_diagnosis()
            else:
                records = self.db.get_diagnosis_by_type(error_type)
            table.setRowCount(len(records))
            table.setProperty("records", records)
            for row_index, record in enumerate(records):
                values = [
                    record["id"], record["created_at"], record["language"],
                    record["error_type"], record["error_line"], record["error_message"],
                ]
                for col, value in enumerate(values):
                    table.setItem(row_index, col, _table_item(value))
            table.clearSelection()
            detail.setHtml("<p>请选择一条样例查看详情。</p>")

        def show_detail(row: int, _column: int) -> None:
            records = table.property("records") or []
            if row >= len(records):
                return
            record = records[row]
            edit_delete_id.setText(str(record["id"]))
            try:
                solutions = json.loads(record.get("solutions") or "[]")
            except json.JSONDecodeError:
                solutions = []
            html_parts = [
                f"<h3>#{record['id']} {html_escape(record['error_type'])} · "
                f"{html_escape(record['language'])}</h3>"
            ]
            html_parts.append("<h4>代码快照</h4>" + _pre(record.get("code", "")))
            for item in solutions:
                html_parts.append(
                    f"<h4>{html_escape(item.get('level', ''))}：{html_escape(item.get('title', ''))}</h4>"
                    f"<p>{html_escape(item.get('detail', ''))}</p>"
                )
            detail.setHtml("".join(html_parts))

        def delete_selected() -> None:
            """支持两种定位方式：输入框填写 ID，或直接选中表格中的一行。"""
            text = edit_delete_id.text().strip()
            if not text:
                selected = table.selectionModel().selectedRows()
                if not selected:
                    QMessageBox.information(dialog, "提示", "请先选中一行，或在输入框填写样例 ID。")
                    return
                records = table.property("records") or []
                row = selected[0].row()
                if row >= len(records):
                    QMessageBox.warning(dialog, "提示", "选中行已失效，请重新选择。")
                    return
                text = str(records[row]["id"])
            try:
                record_id = int(text)
            except ValueError:
                QMessageBox.warning(dialog, "输入无效", "样例 ID 必须是整数。")
                return
            if self.db.get_diagnosis_by_id(record_id) is None:
                QMessageBox.warning(
                    dialog, "未找到样例",
                    f"标识为 {record_id} 的历史样例不存在，可能已被删除。",
                )
                return
            reply = QMessageBox.question(
                dialog, "确认删除",
                f"确定删除标识为 {record_id} 的历史样例吗？此操作不可撤销。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            deleted, message = self.db.delete_diagnosis(record_id)
            if not deleted:
                QMessageBox.warning(dialog, "删除失败", message)
                return
            # 删除后同步三处展示：筛选下拉、表格与详情（数据与界面保持一致）
            self._refresh_history_types()
            current_filter = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("全部类型")
            combo.addItems(getattr(self, "_history_types", []))
            if current_filter != "全部类型" and combo.findText(current_filter) >= 0:
                combo.setCurrentText(current_filter)
            combo.blockSignals(False)
            edit_delete_id.clear()
            reload()
            QMessageBox.information(dialog, "删除成功", message)

        combo.currentTextChanged.connect(lambda _: reload())
        table.cellClicked.connect(show_detail)
        btn_delete.clicked.connect(delete_selected)
        reload()
        dialog.exec_()

    # ------------------------------------------------------------------
    # 模块 B：练习
    # ------------------------------------------------------------------
    def load_questions(self, silent: bool = False) -> None:
        language = self.combo_practice_lang.currentText()
        self.questions = self.db.list_questions(language)
        self.current_index = 0
        if not self.questions:
            self.current_question = None
            self.lbl_progress.setText(f"{language} 题库为空")
            self.lbl_question_title.setText("该语言暂无题目")
            self.lbl_description.setText("可点击上方“开启 AI 更新题库”生成题目。")
            self.lbl_expected.setText("")
            self.answer_editor.setText("")
            self.feedback.setHtml("<p>暂无题目。</p>")
            if not silent:
                QMessageBox.information(self, "题库为空", f"当前没有 {language} 题目，可尝试 AI 更新题库。")
            return
        self._show_question()

    def _show_question(self) -> None:
        if not self.questions:
            return
        self.current_index = max(0, min(self.current_index, len(self.questions) - 1))
        question = self.questions[self.current_index]
        self.current_question = question
        source = question.get("source", "static")
        source_label = {"static": "内置题库", "ai": "AI 生成", "stackoverflow": "Stack Overflow"}.get(source, source)
        self.lbl_progress.setText(f"第 {self.current_index + 1} / {len(self.questions)} 题")
        self.lbl_question_title.setText(
            f"第 {self.current_index + 1} 题：{question['title']}（来源：{source_label}）"
        )
        description = html_escape(question.get("description", ""))
        if question.get("source_url"):
            description += (
                f' <a href="{html_escape(question["source_url"])}">查看 Stack Overflow 原问题</a>'
            )
        self.lbl_description.setText(description.replace("\n", "<br>"))
        self.lbl_expected.setText(
            "期望输出：" + repr(question.get("expected_output", "")) if question.get("expected_output") else "期望输出：空"
        )
        self.answer_editor.setText(question["buggy_code"])
        self.answer_editor.setLanguage(question["language"])
        self.feedback.setHtml("<p>请修改代码后提交，判题依据为真实运行输出。</p>")
        self.correct_view.setHtml("")
        self.correct_view.setVisible(False)
        self.lbl_correct.setVisible(False)
        self.btn_submit.setEnabled(True)

    def next_question(self) -> None:
        if not self.questions:
            return
        if self.current_index >= len(self.questions) - 1:
            QMessageBox.information(self, "提示", "已经是最后一题了。")
            return
        self.current_index += 1
        self._show_question()

    def previous_question(self) -> None:
        if not self.questions:
            return
        self.current_index = max(0, self.current_index - 1)
        self._show_question()

    def submit_answer(self) -> None:
        if not self.current_question:
            QMessageBox.information(self, "提示", "请先加载题目。")
            return
        code = self.answer_editor.text()
        if not code.strip():
            QMessageBox.information(self, "提示", "作答区为空。")
            return
        question = self.current_question
        self.btn_submit.setEnabled(False)
        self.feedback.setHtml("<p>正在受限环境中运行你的代码…</p>")
        self._run_worker(
            judge_answer, self._on_judge_done,
            code, question["language"], question["expected_output"], self.spin_timeout.value(),
            inputs=parse_test_inputs(self.edit_practice_inputs.text()),
            on_error=self._default_worker_error,
        )

    def _on_judge_done(self, data: Dict[str, Any]) -> None:
        self.btn_submit.setEnabled(True)
        question = self.current_question
        if not question:
            return
        passed = bool(data["passed"])
        # 每次作答都写入 practice_history：无论对错都留痕，便于复盘。
        self.db.add_practice(
            question_id=question["id"], language=question["language"],
            title=question["title"], user_code=self.answer_editor.text(),
            passed=passed, actual_output=data["actual"],
            expected_output=data["expected"],
        )
        if passed:
            color = "#18864b"
            self.feedback.setHtml(
                f"<h3 style='color:{color};'>通过！</h3>"
                f"<p>实际输出与期望输出一致。可以点击“下一题”继续。</p>"
            )
            self.correct_view.setVisible(False)
            self.lbl_correct.setVisible(False)
        else:
            reason = "运行超时（超过设定秒数，已被强制终止）" if data["timed_out"] else (
                data["stderr"].strip()[:500] or f"进程返回码 {data['returncode']}"
            )
            self.feedback.setHtml(
                "<h3 style='color:#c0392b;'>未通过</h3>"
                f"<p><b>期望输出：</b>{html_escape(repr(data['expected']))}</p>"
                f"<p><b>实际输出：</b>{html_escape(repr(data['actual']))}</p>"
                f"<p><b>运行诊断：</b>{html_escape(reason)}</p>"
            )
            self.correct_view.setPlainText(question["correct_code"])
            self.correct_view.setVisible(True)
            self.lbl_correct.setVisible(True)

    def show_practice_history(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("作答历史")
        dialog.resize(980, 660)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["题目ID", "时间", "语言", "题目", "结果", "实际输出"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)

        delete_bar = QHBoxLayout()
        edit_delete_question_id = QLineEdit()
        edit_delete_question_id.setPlaceholderText("输入题目 ID；留空则删除上方选中记录所属题目")
        edit_delete_question_id.setFixedWidth(280)
        btn_delete = QPushButton("删除该练习作答历史")
        delete_bar.addWidget(QLabel("题目 ID："))
        delete_bar.addWidget(edit_delete_question_id)
        delete_bar.addWidget(btn_delete)
        delete_bar.addStretch(1)

        detail = QTextBrowser()
        layout.addWidget(table)
        layout.addLayout(delete_bar)
        layout.addWidget(QLabel("详情："))
        layout.addWidget(detail)

        def reload() -> None:
            records = self.db.get_practice_history()
            table.setRowCount(len(records))
            table.setProperty("records", records)
            for row_index, record in enumerate(records):
                values = [
                    record["question_id"], record["created_at"], record["language"],
                    record["title"], "通过" if record["passed"] else "未通过",
                    record["actual_output"],
                ]
                for col, value in enumerate(values):
                    table.setItem(row_index, col, _table_item(value))
            table.clearSelection()
            detail.setHtml("<p>请选择一条作答记录查看详情。</p>")

        def show_detail(row: int, _column: int) -> None:
            records = table.property("records") or []
            if row >= len(records):
                return
            record = records[row]
            edit_delete_question_id.setText(str(record["question_id"]))
            detail.setHtml(
                f"<h3>#{record['question_id']} {html_escape(record['title'])} · "
                f"{html_escape(record['created_at'])}</h3>"
                "<h4>你的代码</h4>" + _pre(record["user_code"]) +
                f"<p><b>判定：</b>{'通过' if record['passed'] else '未通过'}</p>"
                "<h4>期望输出</h4>" + _pre(record["expected_output"] or "")
            )

        def delete_selected() -> None:
            """支持两种定位方式：输入题目 ID，或直接选中该练习的一条作答记录。"""
            text = edit_delete_question_id.text().strip()
            if not text:
                selected = table.selectionModel().selectedRows()
                if not selected:
                    QMessageBox.information(dialog, "提示", "请先选中一行，或在输入框填写题目 ID。")
                    return
                records = table.property("records") or []
                row = selected[0].row()
                if row >= len(records):
                    QMessageBox.warning(dialog, "提示", "选中行已失效，请重新选择。")
                    return
                text = str(records[row]["question_id"])
            try:
                question_id = int(text)
            except ValueError:
                QMessageBox.warning(dialog, "输入无效", "题目 ID 必须是整数。")
                return
            related = self.db.get_practice_by_question(question_id)
            if not related:
                QMessageBox.warning(
                    dialog, "未找到记录",
                    f"题目 ID {question_id} 没有作答历史记录，可能已被删除。",
                )
                return
            reply = QMessageBox.question(
                dialog, "确认删除",
                f"确定删除题目 ID {question_id} 的全部 {len(related)} 条作答历史吗？\n"
                "题目本身、题库配置及其他练习的数据不受影响。此操作不可撤销。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            deleted, message = self.db.delete_practice_by_question(question_id)
            if not deleted:
                QMessageBox.warning(dialog, "删除失败", message)
                return
            edit_delete_question_id.clear()
            reload()
            QMessageBox.information(dialog, "删除成功", message)

        table.cellClicked.connect(show_detail)
        btn_delete.clicked.connect(delete_selected)
        reload()
        dialog.exec_()

    # ------------------------------------------------------------------
    # 模块 C：题库更新
    # ------------------------------------------------------------------
    def ai_update(self) -> None:
        language = self.combo_practice_lang.currentText()
        self.btn_ai_update.setEnabled(False)
        self.statusBar().showMessage("AI 题库更新中…")
        self._run_worker(
            update_questions_from_ai, self._on_ai_done,
            self.db, language, self.edit_api_key.text().strip(),
            self.edit_api_base.text().strip(), self.edit_model.text().strip(),
            self.spin_count.value(), True,
            on_error=self._default_worker_error,
        )

    def _on_ai_done(self, summary: Dict[str, Any]) -> None:
        self.btn_ai_update.setEnabled(True)
        QMessageBox.information(
            self, "AI 题库更新完成",
            f"生成 {summary['generated']} 道，校验通过 {summary['accepted']} 道，"
            f"实际入库 {summary['added']} 道。\n"
            f"被跳过的题目会在校验阶段被过滤，避免错题污染题库。"
        )
        self.load_questions(silent=True)

    def stack_update(self) -> None:
        language = self.combo_practice_lang.currentText()
        self.btn_stack_update.setEnabled(False)
        self.statusBar().showMessage("正在通过 Stack Exchange 官方 API 导入（遵守 crawl-delay，可能较慢）…")
        self._run_worker(
            stack_exchange_update, self._on_stack_done,
            self.db, language, min(self.spin_count.value(), 3),
            on_error=self._default_worker_error,
        )

    def _on_stack_done(self, summary: Dict[str, Any]) -> None:
        self.btn_stack_update.setEnabled(True)
        QMessageBox.information(
            self, "Stack Overflow 导入完成",
            f"入库 {summary['added']} 道，跳过 {summary['skipped']} 道。"
            f"请求间隔 {summary['delay']:.1f} 秒（已读取 robots.txt）。\n"
            "导入题目均标注来源与原问题链接。"
        )
        self.load_questions(silent=True)
