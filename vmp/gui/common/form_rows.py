"""Small form-row builders shared by the main window sidebar."""

from __future__ import annotations

from html import escape

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n import tr

# Total width of a styled QCheckBox indicator (14px box + 1px border each side).
CHECKBOX_INDICATOR_WIDTH = 16


def rich_tooltip(title: str, lines: list[str]) -> str:
    """Build a richer HTML tooltip for cleaner hover presentation."""
    parts: list[str] = []
    for line in lines:
        for chunk in str(line).splitlines():
            parts.append(f"<div style='margin-top:4px;'>{escape(chunk)}</div>")
    body = "".join(parts)
    return (
        "<div style='font-family:Segoe UI; font-size:10pt; color:#f8fafc;'>"
        f"<div style='font-weight:700; margin-bottom:4px;'>{escape(title)}</div>"
        f"{body}"
        "</div>"
    )


def style_spinbox(spinbox: QSpinBox, suffix: str) -> None:
    """Apply common spinbox behavior and suffixes."""
    spinbox.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    spinbox.setSuffix(suffix)
    spinbox.setMinimumHeight(26)


def collapsible_section(title: str) -> tuple[QFrame, QFormLayout]:
    """Create a titled, collapsible sidebar section."""
    section = QFrame()
    section.setObjectName("sideSection")
    layout = QVBoxLayout(section)
    layout.setContentsMargins(10, 9, 10, 10)
    layout.setSpacing(6)
    toggle = QPushButton(f"▾  {title}")
    toggle.setObjectName("sectionToggle")
    toggle.setCheckable(True)
    toggle.setChecked(True)
    toggle.setCursor(Qt.CursorShape.PointingHandCursor)
    toggle.setStyleSheet(
        "QPushButton#sectionToggle { text-align: left; border: none; background: transparent;"
        " color: #111827; font-size: 14px; font-weight: 800; padding: 0 0 2px 0; }"
        "QPushButton#sectionToggle:hover { color: #1d6fe0; }"
    )
    form_container = QWidget()
    form = QFormLayout(form_container)
    form.setContentsMargins(0, 0, 0, 0)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
    form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    form.setHorizontalSpacing(12)
    form.setVerticalSpacing(6)

    def _on_toggled(checked: bool) -> None:
        form_container.setVisible(checked)
        toggle.setText(("▾  " if checked else "▸  ") + title)

    toggle.toggled.connect(_on_toggled)
    layout.addWidget(toggle)
    layout.addWidget(form_container)
    return section, form


def stepper_row(spinbox: QSpinBox) -> QWidget:
    """Build a compact spinbox row with explicit minus/plus buttons."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    minus_button = QPushButton("-")
    plus_button = QPushButton("+")
    for button in (minus_button, plus_button):
        button.setObjectName("stepperButton")
        button.setFixedSize(26, 28)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    minus_button.setToolTip(tr("Wert verringern"))
    plus_button.setToolTip(tr("Wert erhöhen"))
    minus_button.clicked.connect(spinbox.stepDown)
    plus_button.clicked.connect(spinbox.stepUp)
    layout.addWidget(spinbox, 1)
    layout.addWidget(minus_button)
    layout.addWidget(plus_button)
    return row


def stepper_pair_row(label_a: str, spin_a: QSpinBox, label_b: str, spin_b: QSpinBox) -> QWidget:
    """Build a row with two labeled stepper pairs side by side."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    pair_a_widget = QWidget()
    pair_a = QVBoxLayout(pair_a_widget)
    pair_a.setContentsMargins(0, 0, 0, 0)
    pair_a.setSpacing(2)
    label_a_widget = QLabel(label_a)
    label_a_widget.setObjectName("fieldLabel")
    pair_a.addWidget(label_a_widget)
    pair_a.addWidget(stepper_row(spin_a), 1)
    pair_b_widget = QWidget()
    pair_b = QVBoxLayout(pair_b_widget)
    pair_b.setContentsMargins(0, 0, 0, 0)
    pair_b.setSpacing(2)
    label_b_widget = QLabel(label_b)
    label_b_widget.setObjectName("fieldLabel")
    pair_b.addWidget(label_b_widget)
    pair_b.addWidget(stepper_row(spin_b), 1)
    layout.addWidget(pair_a_widget, 1)
    layout.addWidget(pair_b_widget, 1)
    row.second_group = pair_b_widget  # type: ignore[attr-defined]
    return row


def stepper_triple_row(
    label_a: str,
    spin_a: QSpinBox,
    label_b: str,
    spin_b: QSpinBox,
    label_c: str,
    spin_c: QSpinBox,
) -> QWidget:
    """Build a row with three labeled stepper pairs side by side."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for label, spin in ((label_a, spin_a), (label_b, spin_b), (label_c, spin_c)):
        pair = QVBoxLayout()
        pair.setContentsMargins(0, 0, 0, 0)
        pair.setSpacing(2)
        label_widget = QLabel(label)
        label_widget.setObjectName("fieldLabel")
        pair.addWidget(label_widget)
        pair.addWidget(stepper_row(spin), 1)
        layout.addLayout(pair, 1)
    return row


def combo_pair_row(label_a: str, combo_a: QComboBox, label_b: str, combo_b: QComboBox) -> QWidget:
    """Build a row with two labeled comboboxes side by side."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    for label_text, combo in ((label_a, combo_a), (label_b, combo_b)):
        pair_widget = QWidget()
        pair = QVBoxLayout(pair_widget)
        pair.setContentsMargins(0, 0, 0, 0)
        pair.setSpacing(2)
        label_widget = QLabel(label_text)
        label_widget.setObjectName("fieldLabel")
        pair.addWidget(label_widget)
        pair.addWidget(combo, 1)
        layout.addWidget(pair_widget, 1)
    return row


def checkbox_pair_row(check_a: QCheckBox, check_b: QCheckBox) -> QWidget:
    """Build a row with two checkboxes side by side."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    layout.addWidget(wrapping_checkbox(check_a), 1)
    layout.addWidget(wrapping_checkbox(check_b), 1)
    return row


def label_info_row(label_text: str, tooltip: str) -> QWidget:
    """Build a compact label with an inline info icon."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    label = QLabel(label_text)
    label.setObjectName("fieldLabel")
    info = QLabel("i")
    info.setObjectName("infoButton")
    info.setAlignment(Qt.AlignmentFlag.AlignCenter)
    info.setFixedSize(18, 18)
    info.setToolTip(rich_tooltip(label_text, [tooltip]))
    layout.addWidget(label)
    layout.addWidget(info)
    layout.addStretch(1)
    return row


def wrapping_checkbox(checkbox: QCheckBox) -> QWidget:
    """Wrap a checkbox so its label can break onto multiple lines.

    A plain ``QCheckBox`` reports its full one-line text width as its minimum
    size, which would force the whole sidebar wider than needed. We move the
    text into a word-wrapping ``QLabel`` next to a textless checkbox and let
    clicks on the label toggle the box.
    """
    text = checkbox.text()
    checkbox.setText("")
    checkbox.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    # A textless QCheckBox still reserves its indicator-to-text spacing (6px)
    # as trailing width, which would push the label further from the indicator
    # than a native checkbox. Pin the checkbox to the indicator width so the
    # gap to the label matches a plain QCheckBox exactly.
    checkbox.setFixedWidth(CHECKBOX_INDICATOR_WIDTH)
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    label = QLabel(text)
    label.setObjectName("checkLabel")
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def _toggle(_event: object, box: QCheckBox = checkbox) -> None:
        if box.isEnabled():
            box.toggle()

    label.mousePressEvent = _toggle  # type: ignore[method-assign]
    layout.addWidget(checkbox, 0, Qt.AlignmentFlag.AlignTop)
    layout.addWidget(label, 1)
    return container


def checkbox_with_info(checkbox: QCheckBox, tooltip: str) -> QWidget:
    """Build a checkbox row whose info icon sits directly after the label text.

    The label sizes to its content so the info icon follows the text instead of
    being pushed to the far right. Long labels wrap within a bounded width so a
    single long option cannot widen the whole sidebar.
    """
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    title = checkbox.text()
    checkbox.setText("")
    checkbox.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    checkbox.setFixedWidth(CHECKBOX_INDICATOR_WIDTH)
    label = QLabel(title)
    label.setObjectName("checkLabel")
    # Single line, sized to the text, so the info icon follows the text
    # directly instead of being pushed to the right by a filling label. These
    # option labels comfortably fit the sidebar width, so no wrapping is needed.
    label.setWordWrap(False)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def _toggle(_event: object, box: QCheckBox = checkbox) -> None:
        if box.isEnabled():
            box.toggle()

    label.mousePressEvent = _toggle  # type: ignore[method-assign]
    info = QLabel("i")
    info.setObjectName("infoButton")
    info.setAlignment(Qt.AlignmentFlag.AlignCenter)
    info.setFixedSize(18, 18)
    info.setToolTip(rich_tooltip(title, [tooltip]))
    layout.addWidget(checkbox, 0, Qt.AlignmentFlag.AlignTop)
    layout.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
    layout.addWidget(info, 0, Qt.AlignmentFlag.AlignVCenter)
    layout.addStretch(1)
    return row
