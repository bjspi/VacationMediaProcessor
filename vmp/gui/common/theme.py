"""Application theme: stylesheet, icon paths, and small style tweaks."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QProxyStyle, QStyle

# Milliseconds before a tooltip appears on hover. Lower than Qt's ~700ms default
# so the info/overlay hints show up a bit quicker.
TOOLTIP_WAKEUP_DELAY_MS = 250

# This file lives at vmp/gui/common/theme.py, so the
# package directory is three levels up. If this module ever moves to a
# different folder depth, this constant must be adjusted — tests/test_assets.py
# guards every referenced icon path and fails loudly on a mismatch.
_PACKAGE_DIR = Path(__file__).resolve().parents[2]


def app_icon_path() -> Path:
    """Return the application icon path (bundled with the package assets)."""
    return _PACKAGE_DIR / "assets" / "icon.png"


def asset_path(name: str) -> Path:
    """Return the path of a bundled asset (SVG icons in the package's assets dir)."""
    return _PACKAGE_DIR / "assets" / name


class FastTooltipStyle(QProxyStyle):
    """Proxy style that only shortens the tooltip wake-up delay.

    Wraps the application's existing style, so widget rendering is unchanged; it
    just returns a smaller value for the tooltip wake-up style hint.
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # type: ignore[override]
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return TOOLTIP_WAKEUP_DELAY_MS
        return super().styleHint(hint, option, widget, returnData)


_MAIN_STYLESHEET = """
    QMainWindow, QWidget { background: #eef1f5; color: #1e242c; font-size: 13px; }
    QLabel, QCheckBox { background: transparent; }
    QMenuBar {
        background: #f9fafb; border-bottom: 1px solid #cfd6e3;
        padding: 2px 4px; spacing: 2px;
    }
    QMenuBar::item {
        background: transparent; color: #1f2937;
        padding: 5px 10px; border-radius: 4px;
    }
    QMenuBar::item:selected {
        background: #e8edf4; color: #111827;
    }
    QMenuBar::item:pressed {
        background: #dfe6ef;
    }
    QMenu {
        background: #ffffff; color: #1f2937;
        border: 1px solid #b8c2d0; padding: 4px 0;
    }
    QMenu::item {
        padding: 6px 22px 6px 28px; background: transparent;
    }
    QMenu::icon {
        left: 10px;
    }
    QMenu::item:selected {
        background: #e8f1ff; color: #111827;
    }
    QMenu::separator {
        height: 1px; background: #e5e7eb; margin: 4px 8px;
    }
    #header {
        background: #ffffff; border: 1px solid #d6dce6; border-radius: 10px;
    }
    #logo {
        background: #eef6ff; border: 1px solid #cfdef3; border-radius: 10px;
    }
    #appTitle { font-size: 21px; font-weight: 800; color: #111827; }
    #appSubtitle { color: #546171; }
    #folderLabel {
        color: #1f6feb; font-size: 12px; font-weight: 700;
        padding-top: 2px;
    }
    #progressPanel {
        background: #ffffff; border: 1px solid #d6dce6; border-radius: 8px;
    }
    #statusLabel { color: #526071; font-size: 12px; }
    QPushButton {
        background: #2f6fed; color: white; border: 0; border-radius: 7px;
        padding: 8px 11px; font-weight: 700;
    }
    QPushButton[headerAction="true"] {
        padding: 0; min-width: 44px; max-width: 44px;
    }
    QPushButton:disabled { background: #c7ced8; color: #6b7280; }
    QPushButton:hover:!disabled { background: #245fd4; }
    #primaryButton { background: #0f766e; color: #ffffff; }
    #primaryButton:hover:!disabled { background: #0b665f; }
    #secondaryButton { background: #2f6fed; }
    #secondaryButton:disabled {
        background: #e5eaf1; color: #7b8796; border: 1px solid #d1d8e3;
    }
    #runButton { background: #178c54; color: #ffffff; }
    #runButton:hover:!disabled { background: #0f7a46; }
    #runButton:disabled {
        background: #e5eaf1; color: #7b8796; border: 1px solid #d1d8e3;
    }
    #ghostButton {
        background: #f8fafc; color: #1e293b; border: 1px solid #cfd6e3;
    }
    #ghostButton:hover:!disabled { background: #edf2f7; }
    #ghostButton:disabled {
        background: #f4f6f9; color: #8b95a4; border: 1px solid #d8dee8;
    }
    #badgeBubble {
        background: #dc2626; color: #ffffff;
        border: 2px solid #ffffff;
        border-radius: 9px;
        font-size: 9px;
        font-weight: 800;
    }
    #miniButton {
        background: #f8fafc; color: #1e293b; border: 1px solid #cfd6e3;
        padding: 5px 0; font-weight: 800;
    }
    #assistantButton {
        background: #eef6f3; color: #0f3f37; border: 1px solid #bcd9d1;
        padding: 6px 0; font-weight: 800;
    }
    #assistantButton:hover:!disabled { background: #dff0eb; }
    #infoButton {
        background: #eef6ff; color: #1f4f9b; border: 1px solid #b8cae8;
        border-radius: 9px; padding: 0; font-weight: 800;
    }
    QLabel#infoButton:hover { background: #dceaff; border-color: #86a8dd; }
    QToolTip {
        background: #111827; color: #f8fafc; border: 1px solid #374151;
        padding: 8px 10px; border-radius: 8px;
    }
    #stepperButton {
        background: #f8fafc; color: #1e293b; border: 1px solid #c8d2df;
        border-radius: 6px; padding: 0; font-size: 15px; font-weight: 900;
    }
    #stepperButton:hover:!disabled {
        background: #edf4ff; border-color: #93b4e8; color: #0f3f91;
    }
    #stepperButton:pressed {
        background: #dbeafe;
    }
    #sideScroll { background: transparent; border: 0; }
    QProgressBar {
        border: 1px solid #cfd6e3; border-radius: 6px; background: #f8fafc;
        height: 13px; text-align: center;
    }
    QProgressBar::chunk { background: #1f9d55; border-radius: 6px; }
    #tablePanel {
        background: #ffffff; border: 1px solid #d6dce6; border-radius: 8px;
    }
    QTableWidget {
        background: #ffffff; alternate-background-color: #f8fafc;
        border: 0; gridline-color: #eef1f5; border-radius: 8px;
        selection-background-color: #dbeafe; selection-color: #0f172a;
    }
    QHeaderView::section {
        background: #f1f5f9; border: 0; border-right: 1px solid #d9dee7;
        padding: 8px; font-weight: 800; color: #334155;
    }
    #sideSection, #preview {
        background: #ffffff; border: 1px solid #d6dce6; border-radius: 8px;
    }
    #sectionTitle {
        color: #111827; font-size: 14px; font-weight: 800;
        padding-bottom: 2px;
    }
    #sectionHint {
        color: #667085; font-size: 12px;
        padding-bottom: 2px;
    }
    #fieldLabel {
        color: #667085; font-size: 11px;
        padding-bottom: 0px;
    }
    QTextEdit, QLineEdit, QSpinBox, QComboBox {
        background: #ffffff; border: 1px solid #cfd6e3; border-radius: 6px;
        min-height: 24px; padding: 3px 6px;
    }
    QSpinBox::up-button, QSpinBox::down-button {
        width: 0; border: 0;
    }
    QCheckBox { spacing: 6px; color: #1f2937; }
    QCheckBox::indicator {
        width: 14px; height: 14px; border: 1px solid #8fa0b5; border-radius: 3px;
        background: #ffffff;
    }
    QCheckBox::indicator:checked {
        background: #2f6fed; border-color: #2f6fed;
        image: url("__CHECK_ICON__");
    }
    QCheckBox::indicator:checked:hover {
        background: #245fd4; border-color: #245fd4;
    }
    QCheckBox::indicator:unchecked:hover {
        border-color: #2f6fed;
    }
    """


def main_window_stylesheet() -> str:
    """Return the main window stylesheet with resolved asset paths."""
    return _MAIN_STYLESHEET.replace("__CHECK_ICON__", asset_path("check.svg").as_posix())
