"""Controller for the main media table: population, filtering, and column fitting.

The controller owns the ``QTableWidget`` and everything view-related (cell
values, fonts, per-column filters, width distribution). Plan/result state stays
on the ``MainWindow``, which the controller reads through a back-reference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ...core.i18n import tr
from ..common.form_rows import rich_tooltip
from ..common.theme import asset_path
from ..common.widgets import (
    COLUMN_CAP,
    COLUMN_FIT,
    FilterHeaderView,
    MediaTableColumns,
    MediaTableWidget,
    distribute_column_widths,
)
from ..common.plan_display import codec_cell_text, file_size_text, human_size, video_bucket_label_text
from ...metadata import gps_coordinates, has_exif_datetime_values, has_gps
from ...core.models import MediaPlan, PlanStatus
from ...reports import plan_action_summary

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .window import MainWindow


class MediaTableController:
    """Owns the media table widget, its filters, fonts, and column fitting."""

    def __init__(self, window: "MainWindow") -> None:
        self._win = window
        self._column_filters: dict[int, dict] = {}
        self._col_desired: dict[str, int] = {}
        self.table = MediaTableWidget(0, len(MediaTableColumns.ORDER))
        filter_header = FilterHeaderView(Qt.Orientation.Horizontal, self.table)
        filter_header.filterClicked.connect(self.show_column_filter)
        self.table.setHorizontalHeader(filter_header)
        self.table.setHorizontalHeaderLabels(MediaTableColumns.labels())
        for column in range(self.table.columnCount()):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    # -- fonts / geometry ---------------------------------------------------- #

    def apply_font_size(self, size: int) -> None:
        """Apply a specific font size to the table."""
        font = self.table.font()
        font.setPointSize(size)
        self.table.setStyleSheet(
            "QTableWidget { font-size: %dpt; }"
            "QTableWidget::item { font-size: %dpt; }" % (size, size)
        )
        self.table.setFont(font)
        self.table.horizontalHeader().setFont(font)
        self.table.verticalHeader().setFont(font)
        self.table.horizontalHeader().setStyleSheet("QHeaderView::section { font-size: %dpt; }" % size)
        row_height = max(24, self.table.fontMetrics().height() + 10)
        self.table.verticalHeader().setDefaultSectionSize(row_height)
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, row_height)
            for column in range(self.table.columnCount()):
                item = self.table.item(row, column)
                if item is not None:
                    item.setFont(font)
        self.table.viewport().update()

    def recompute_content_widths(self) -> None:
        """Cache each column's content-based desired width (header + widest cell).

        Scans a bounded sample of rows so huge folders stay fast; the result is
        reused by :meth:`fit_columns` on every resize without re-scanning.
        """
        font_metrics = self.table.fontMetrics()
        sample = min(self.table.rowCount(), 400)
        desired: dict[str, int] = {}
        for name in MediaTableColumns.ORDER:
            column = MediaTableColumns.index(name)
            width = font_metrics.horizontalAdvance(tr(MediaTableColumns.LABELS[name])) + 24
            for row in range(sample):
                item = self.table.item(row, column)
                if item is not None and item.text():
                    width = max(width, font_metrics.horizontalAdvance(item.text()) + 24)
            desired[name] = width
        self._col_desired = desired

    def fit_columns(self) -> None:
        """Distribute the available width across columns, shrinking by priority.

        Columns get their content-desired width when everything fits; otherwise
        the high-shrink-priority columns give up width first (text elides) so the
        table fits its panel without a horizontal scrollbar. Spare width goes to
        the action column.
        """
        if self.table.columnCount() == 0:
            return
        viewport_width = self.table.viewport().width()
        if viewport_width <= 1:
            return  # not laid out yet; re-applied on show/resize
        frame_width = self.table.frameWidth() * 2
        vheader = self.table.verticalHeader().width() if self.table.verticalHeader().isVisible() else 0
        scrollbar = (
            self.table.verticalScrollBar().sizeHint().width()
            if self.table.verticalScrollBar().isVisible()
            else 0
        )
        available = viewport_width - frame_width - vheader - scrollbar
        if available <= 0:
            return
        font_metrics = self.table.fontMetrics()
        desired: dict[str, int] = {}
        mins: dict[str, int] = {}
        priorities: dict[str, int] = {}
        for name, (minimum, priority) in COLUMN_FIT.items():
            base = self._col_desired.get(name)
            if base is None:
                base = font_metrics.horizontalAdvance(tr(MediaTableColumns.LABELS[name])) + 24
            cap = COLUMN_CAP.get(name)
            if cap is not None:
                base = min(base, cap)
            desired[name] = max(base, minimum)
            mins[name] = minimum
            priorities[name] = priority
        widths = distribute_column_widths(desired, mins, priorities, available, absorber="action")
        for name, width in widths.items():
            self.table.setColumnWidth(MediaTableColumns.index(name), int(width))

    # -- population ---------------------------------------------------------- #

    def populate(self) -> None:
        """Fill the media table from the window's plans."""
        window = self._win
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(window.plans))
            for row, plan in enumerate(window.plans):
                values, action_summary = self.values_for_plan(plan)
                for column_name in MediaTableColumns.ORDER:
                    self._set_item(row, column_name, values[column_name], plan, action_summary)
            self.apply_font_size(window.settings_model.table_font_size)
            self.recompute_content_widths()
            self.fit_columns()
            window._rebuild_row_map()
        finally:
            self.table.setUpdatesEnabled(True)
        self.apply_filters()
        window._update_stats()
        window._update_missing_exif_badge()
        window._update_pairs_badge()

    def refresh_row(self, row: int) -> None:
        """Refresh visible cells for one plan row."""
        window = self._win
        if row < 0 or row >= len(window.plans):
            return
        if row >= self.table.rowCount():
            return
        plan = window.plans[row]
        values, action_summary = self.values_for_plan(plan)
        self.table.setUpdatesEnabled(False)
        try:
            for column_name in MediaTableColumns.ORDER:
                self._set_item(row, column_name, values[column_name], plan, action_summary)
        finally:
            self.table.setUpdatesEnabled(True)
        row_rect = QRect(0, self.table.rowViewportPosition(row), self.table.viewport().width(), self.table.rowHeight(row))
        self.table.viewport().update(row_rect)

    def refresh_workflow_columns(self) -> None:
        """Refresh only table cells affected by workflow setting changes."""
        columns = ("action", "target", "bucket")
        self.table.setUpdatesEnabled(False)
        try:
            for row, plan in enumerate(self._win.plans):
                values, action_summary = self.values_for_plan(plan)
                for column_name in columns:
                    self._set_item(row, column_name, values[column_name], plan, action_summary)
        finally:
            self.table.setUpdatesEnabled(True)
        self.apply_filters()
        self.table.viewport().update()

    def values_for_plan(self, plan: MediaPlan) -> tuple[dict[str, str], str]:
        """Return visible table values for one plan."""
        window = self._win
        result = plan.analysis
        action_summary = plan_action_summary(plan)
        bucket = video_bucket_label_text(result, window.settings_model)
        size = file_size_text(result.item.path)
        if result.status == PlanStatus.DONE and result.item.path in window._original_sizes:
            orig = window._original_sizes[result.item.path]
            try:
                new_size = result.item.path.stat().st_size
            except OSError:
                new_size = 0
            if orig > 0:
                pct = (new_size - orig) / orig * 100
                sign = "+" if pct >= 0 else ""
                size = f"{human_size(new_size)} ({sign}{pct:.0f}%)"
            else:
                size = human_size(new_size)
        return (
            {
                "status": result.status.value.upper(),
                "type": result.item.kind.value,
                "file": str(result.item.relative_path),
                "date": result.resolved.local_dt.strftime("%Y-%m-%d %H:%M:%S") if result.resolved.local_dt else "",
                "gps": "✓" if has_gps(result.metadata.tags) else "",
                "source": result.resolved.source,
                "conf": result.resolved.confidence.value.capitalize(),
                "action": action_summary,
                "target": str(plan.final_path.name) if plan.final_path else "",
                "bucket": bucket,
                "codec": codec_cell_text(result),
                "size": size,
            },
            action_summary,
        )

    def _set_item(
        self,
        row: int,
        column_name: str,
        value: str,
        plan: MediaPlan,
        action_summary: str,
    ) -> None:
        """Set one table item using the common formatting rules."""
        column = MediaTableColumns.index(column_name)
        item = QTableWidgetItem(value)
        if column_name == "status":
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if column_name == "gps":
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            # value is non-empty only when has_gps() matched, so the (regex-based)
            # coordinate parse can be skipped for the majority of rows.
            coords = gps_coordinates(plan.analysis.metadata.tags) if value else None
            if coords is not None:
                item.setForeground(QColor("#1a7f37"))
                item.setToolTip(
                    tr("GPS: {latitude:.6f}, {longitude:.6f}\nDoppelklick öffnet Google Maps.").format(latitude=coords[0], longitude=coords[1])
                )
        if column_name == "conf":
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            conf_colors = {"Zero": "#7f1d1d", "Low": "#b42318", "Medium": "#b54708", "High": "#1a7f37"}
            colour = conf_colors.get(value)
            if colour is not None:
                item.setForeground(QColor(colour))
            warnings = plan.analysis.resolved.warnings
            tip_lines = [f"Confidence: {value}", tr("Quelle: {source}").format(source=plan.analysis.resolved.source)]
            if warnings:
                tip_lines.extend(warnings)
            item.setToolTip(rich_tooltip("Confidence", tip_lines))
        if column_name == "action":
            result = plan.analysis
            action_lines = [f"{idx + 1}. {action.description}" for idx, action in enumerate(plan.actions)] if plan.actions else [action_summary]
            if "System:FileName" in result.resolved.source and not has_exif_datetime_values(result.metadata):
                action_lines.append(tr("Datumsangabe aus Dateiname geparst, weil kein nutzbares EXIF-Datum vorhanden war."))
            item.setToolTip(rich_tooltip(tr("Aktion"), action_lines))
        self.table.setItem(row, column, item)

    # -- filtering / selection ------------------------------------------------ #

    def selected_visible_rows(self) -> list[int]:
        """Return selected table rows that are currently visible after filtering."""
        return sorted(
            {
                index.row()
                for index in self.table.selectionModel().selectedRows()
                if 0 <= index.row() < len(self._win.plans) and not self.table.isRowHidden(index.row())
            }
        )

    def distinct_values(self, column: int) -> list[str]:
        """Return the sorted distinct cell texts currently present in a column."""
        values = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, column)
            values.add(item.text() if item is not None else "")
        return sorted(values, key=lambda value: value.lower())

    def show_column_filter(self, column: int) -> None:
        """Show an Excel-style filter popup for one table column."""
        state = self._column_filters.get(column, {"excluded": set(), "text": ""})
        excluded = set(state.get("excluded", set()))
        menu = QMenu(self.table)
        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.setSpacing(6)
        title = QLabel(tr(MediaTableColumns.LABELS.get(MediaTableColumns.ORDER[column], "")) if column < len(MediaTableColumns.ORDER) else "")
        title.setObjectName("fieldLabel")
        vbox.addWidget(title)
        search = QLineEdit()
        search.setPlaceholderText(tr("Enthält…"))
        search.setText(state.get("text", ""))
        vbox.addWidget(search)
        list_widget = QListWidget()
        list_widget.setMinimumWidth(220)
        list_widget.setMaximumHeight(280)
        check_dark = asset_path("check_dark.svg").as_posix()
        list_widget.setStyleSheet(
            "QListWidget::indicator { width: 14px; height: 14px; border: 1px solid #8fa0b5;"
            " border-radius: 3px; background: #ffffff; }"
            f'QListWidget::indicator:checked {{ image: url("{check_dark}"); }}'
        )
        for value in self.distinct_values(column):
            entry = QListWidgetItem(tr("(leer)") if value == "" else value)
            entry.setData(Qt.ItemDataRole.UserRole, value)
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(Qt.CheckState.Unchecked if value in excluded else Qt.CheckState.Checked)
            list_widget.addItem(entry)
        vbox.addWidget(list_widget)

        def _filter_list(text: str) -> None:
            needle = text.lower()
            for i in range(list_widget.count()):
                row_item = list_widget.item(i)
                row_item.setHidden(needle not in row_item.text().lower())

        search.textChanged.connect(_filter_list)

        def _set_all(checked: bool) -> None:
            for i in range(list_widget.count()):
                row_item = list_widget.item(i)
                if not row_item.isHidden():
                    row_item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

        button_row = QHBoxLayout()
        select_all = QPushButton(tr("Alle"))
        select_none = QPushButton(tr("Keine"))
        select_all.clicked.connect(lambda: _set_all(True))
        select_none.clicked.connect(lambda: _set_all(False))
        button_row.addWidget(select_all)
        button_row.addWidget(select_none)
        button_row.addStretch(1)
        vbox.addLayout(button_row)

        action_row = QHBoxLayout()
        apply_button = QPushButton(tr("Anwenden"))
        reset_button = QPushButton(tr("Zurücksetzen"))
        action_row.addWidget(reset_button)
        action_row.addStretch(1)
        action_row.addWidget(apply_button)
        vbox.addLayout(action_row)

        def _apply() -> None:
            new_excluded = {
                list_widget.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(list_widget.count())
                if list_widget.item(i).checkState() == Qt.CheckState.Unchecked
            }
            text_value = search.text().strip()
            if new_excluded or text_value:
                self._column_filters[column] = {"excluded": new_excluded, "text": text_value}
            else:
                self._column_filters.pop(column, None)
            self.apply_filters()
            menu.close()

        def _reset() -> None:
            self._column_filters.pop(column, None)
            self.apply_filters()
            menu.close()

        apply_button.clicked.connect(_apply)
        reset_button.clicked.connect(_reset)

        widget_action = QWidgetAction(menu)
        widget_action.setDefaultWidget(container)
        menu.addAction(widget_action)
        header = self.table.horizontalHeader()
        position = header.mapToGlobal(QPoint(header.sectionViewportPosition(column), header.height()))
        menu.exec(position)

    def apply_filters(self) -> None:
        """Hide rows that do not match the active per-column filters."""
        filters = self._column_filters
        for row in range(self.table.rowCount()):
            hidden = False
            for column, state in filters.items():
                item = self.table.item(row, column)
                text = item.text() if item is not None else ""
                excluded = state.get("excluded")
                if excluded and text in excluded:
                    hidden = True
                    break
                needle = state.get("text", "")
                if needle and needle.lower() not in text.lower():
                    hidden = True
                    break
            self.table.setRowHidden(row, hidden)
        header = self.table.horizontalHeader()
        if isinstance(header, FilterHeaderView):
            header.set_active_columns(set(filters.keys()))
