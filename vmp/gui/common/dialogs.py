"""Small auxiliary dialogs for the main window."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n import tr


def show_missing_exif_dialog(parent: QWidget | None, rows: list[list[str]]) -> None:
    """Show the review dialog for files without core EXIF datetime values."""
    headers = [tr("Datei"), tr("Typ"), tr("Erkannt"), "Confidence", tr("Vorschlag")]
    dialog = QDialog(parent)
    dialog.setWindowTitle("Missing EXIF Review")
    dialog.resize(820, 520)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(10)
    summary = QLabel(tr("{count} Datei(en) ohne core EXIF-Datum gefunden.").format(count=len(rows)))
    summary.setObjectName("statusLabel")
    layout.addWidget(summary)
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            table.setItem(row_index, col_index, QTableWidgetItem(value))
    layout.addWidget(table, 1)
    close_button = QPushButton(tr("Schließen"))
    close_button.setObjectName("secondaryButton")
    close_button.clicked.connect(dialog.accept)
    layout.addWidget(close_button)
    dialog.exec()
