# -*- coding: utf-8 -*-
"""
main.py —— 程序入口

安装依赖：
    pip install -r requirements.txt
    # 或手动：
    pip install PyQt5 QScintilla
    pip install coex        # 可选；不装则自动降级为 subprocess 执行器

运行：
    python main.py
"""

from __future__ import annotations

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication, QMessageBox

from ui_main import QSCI_AVAILABLE, QSCI_ERROR, MainWindow


STYLE_SHEET = """
QWidget { font-family: "Microsoft YaHei", "Segoe UI"; font-size: 10pt; }
QMainWindow { background: #f5f8fc; }
QTabWidget::pane { border: 1px solid #d6dee9; background: #ffffff; }
QTabBar::tab { padding: 7px 18px; background: #eaf0f8; color: #24415f; }
QTabBar::tab:selected { background: #ffffff; color: #1f5fd0; font-weight: bold; }
QPushButton {
    background: #2f6fed; color: #ffffff; border: none;
    border-radius: 4px; padding: 6px 12px;
}
QPushButton:hover { background: #255fd4; }
QPushButton:disabled { background: #a9bdd8; color: #eef2f8; }
QLineEdit, QComboBox, QSpinBox {
    background: #ffffff; border: 1px solid #c8d2e0;
    border-radius: 3px; padding: 3px 6px;
}
QTextBrowser { background: #ffffff; border: 1px solid #d6dee9; }
QGroupBox { border: 1px solid #c8d2e0; border-radius: 4px; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #2f6fed; }
QTableWidget { background: #ffffff; alternate-background-color: #f3f7fd; }
QHeaderView::section { background: #eaf0f8; padding: 4px; border: 1px solid #d6dee9; }
"""


def main() -> None:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei", 10))
    app.setStyleSheet(STYLE_SHEET)

    window = MainWindow()
    window.show()
    if not QSCI_AVAILABLE:
        QMessageBox.information(
            window,
            "编辑器降级提示",
            "未检测到 QScintilla，已使用 QPlainTextEdit 降级显示，功能仍可用。\n\n"
            "建议执行：pip install QScintilla\n"
            f"导入错误：{QSCI_ERROR[:200]}",
        )
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
