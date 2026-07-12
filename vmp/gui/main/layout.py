"""Top-level layout construction for the main window (menus, panels, log dock)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .header import build_header
from .media_table import MediaTableController
from .preview_pane import PreviewController
from ..common.widgets import AspectRatioPreview
from .workflow_panel import build_workflow_section
from ...core.i18n import tr
from ...core.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .window import MainWindow

LOGGER = get_logger(__name__)

def build_ui(window: "MainWindow") -> None:
    """Build widgets and layout."""
    open_action = QAction(tr("Ordner öffnen"), window)
    open_action.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
    open_action.triggered.connect(window.choose_folder)
    window.open_action = open_action
    open_settings_action = QAction(tr("Settings öffnen ..."), window)
    open_settings_action.setIcon(window._asset_icon("gear.svg"))
    open_settings_action.triggered.connect(window.open_settings_dialog)
    save_action = QAction(tr("Aktuelle Einstellungen speichern"), window)
    save_action.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
    save_action.triggered.connect(window.save_current_settings)
    open_log_action = QAction(tr("Logfile öffnen"), window)
    open_log_action.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogInfoView))
    open_log_action.triggered.connect(window.open_logfile)
    toggle_log_action = QAction(tr("Log-Panel"), window)
    toggle_log_action.setCheckable(True)
    toggle_log_action.setChecked(False)
    toggle_log_action.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
    toggle_log_action.triggered.connect(window._toggle_log_dock)
    window._toggle_log_action = toggle_log_action
    xnconvert_action = QAction(tr("XnConvert öffnen"), window)
    xnconvert_action.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
    xnconvert_action.triggered.connect(
        lambda: window.launch_configured_tool(window.settings_model.tools.xnconvert_gui, "XnConvert", False)
    )
    xnview_action = QAction(tr("XnView MP öffnen"), window)
    xnview_action.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView))
    xnview_action.triggered.connect(
        lambda: window.launch_configured_tool(window.settings_model.tools.xnviewmp, "XnView MP", True)
    )
    shutter_action = QAction(tr("Shutter Encoder öffnen"), window)
    shutter_action.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
    shutter_action.triggered.connect(
        lambda: window.launch_configured_tool(window.settings_model.tools.shutter_encoder, "Shutter Encoder", False)
    )
    window.menuBar().addMenu(tr("Datei")).addAction(open_action)
    tools_menu = window.menuBar().addMenu("Tools")
    tools_menu.addAction(xnconvert_action)
    tools_menu.addAction(xnview_action)
    tools_menu.addAction(shutter_action)
    tools_menu.addSeparator()
    tools_menu.addAction(toggle_log_action)
    tools_menu.addAction(open_log_action)
    settings_menu = window.menuBar().addMenu("Settings")
    settings_menu.addAction(open_settings_action)
    settings_menu.addSeparator()
    settings_menu.addAction(save_action)

    root_widget = QWidget()
    main_layout = QHBoxLayout(root_widget)
    main_layout.setContentsMargins(16, 16, 16, 16)
    main_layout.setSpacing(12)

    left_widget = QWidget()
    left_layout = QVBoxLayout(left_widget)
    left_layout.setContentsMargins(0, 0, 0, 0)
    left_layout.setSpacing(12)
    left_layout.addWidget(build_header(window))

    progress_panel = QFrame()
    progress_panel.setObjectName("progressPanel")
    progress_layout = QVBoxLayout(progress_panel)
    progress_layout.setContentsMargins(12, 8, 12, 10)
    progress_layout.setSpacing(6)
    window.progress = QProgressBar()
    window.progress.setRange(0, 100)
    window.progress.setValue(0)
    window.status_label = QLabel(tr("Bereit. Settings rechts kannst du schon vorbereiten."))
    window.status_label.setObjectName("statusLabel")
    progress_layout.addWidget(window.progress)
    progress_layout.addWidget(window.status_label)
    left_layout.addWidget(progress_panel)
    left_layout.addWidget(build_table_panel(window), 1)

    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(left_widget)
    side_panel = build_side_panel(window)
    splitter.addWidget(side_panel)
    window._main_splitter = splitter
    side_width = getattr(window, "_side_panel_fit_width", 450)
    splitter.setSizes([max(640, 1380 - side_width), side_width])
    splitter.setStretchFactor(0, 1)
    splitter.setStretchFactor(1, 0)
    splitter.splitterMoved.connect(lambda *_: window._fit_table_columns())
    main_layout.addWidget(splitter, 1)
    window.setCentralWidget(root_widget)
    build_status_bar(window)

def build_status_bar(window: "MainWindow") -> None:
    """Build the bottom status bar with media counts and sizes."""
    window.stats_label = QLabel(tr("Bereit"))
    window.stats_label.setObjectName("statusLabel")
    bar = QStatusBar(window)
    bar.addWidget(window.stats_label, 1)
    window.setStatusBar(bar)

def build_table_panel(window: "MainWindow") -> QWidget:
    """Build the media table panel around the table controller's widget."""
    panel = QFrame()
    panel.setObjectName("tablePanel")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    window._table_controller = MediaTableController(window)
    window.table = window._table_controller.table
    window.table.deletePressed.connect(window.remove_selected_rows_from_table)
    window.table.itemSelectionChanged.connect(window.update_selection)
    window.table.cellDoubleClicked.connect(window._on_table_double_clicked)
    window.table.customContextMenuRequested.connect(window._show_table_context_menu)
    window._apply_table_font()
    layout.addWidget(window.table)
    return panel

def build_side_panel(window: "MainWindow") -> QWidget:
    """Build the settings and preview side panel."""
    panel = QScrollArea()
    panel.setWidgetResizable(True)
    panel.setFrameShape(QFrame.Shape.NoFrame)
    panel.setMinimumWidth(300)
    panel.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    panel.setObjectName("sideScroll")
    window._side_scroll = panel
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)

    layout.addWidget(build_workflow_section(window))

    window.preview = AspectRatioPreview()
    layout.addWidget(window.preview, 3)

    window.details = QTextEdit()
    window.details.setReadOnly(True)
    layout.addWidget(window.details, 2)

    window._preview_controller = PreviewController(window.preview, lambda: window.settings_model.tools.ffmpeg)
    panel.setWidget(content)
    # Initial estimate; refined in _fit_side_panel() once styling/fonts are applied.
    window._fit_side_panel()
    return panel

def build_log_dock(window: "MainWindow") -> QDockWidget:
    """Build the collapsible dock widget for live log output."""
    dock = QDockWidget("Log", window)
    dock.setObjectName("logDock")
    log_text = QPlainTextEdit()
    log_text.setReadOnly(True)
    log_text.setMaximumBlockCount(10_000)
    log_text.setFont(QFont("Consolas", 9))
    log_text.setStyleSheet(
        "QPlainTextEdit {"
        "  background: #1e1e1e; color: #d4d4d4;"
        "  border: 1px solid #3c3c3c;"
        "}"
    )
    dock.setWidget(log_text)
    dock.setAllowedAreas(
        Qt.DockWidgetArea.BottomDockWidgetArea
        | Qt.DockWidgetArea.RightDockWidgetArea
    )
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    dock.hide()
    # Sync menu toggle when dock is shown/hidden by the user
    dock.visibilityChanged.connect(window._on_log_dock_visibility)
    window.log_dock = dock
    window.log_text = log_text
    return dock
