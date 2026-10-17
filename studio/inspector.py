from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSignalBlocker, Signal, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class ColorField(QWidget):
    changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.edit = QLineEdit()
        self.button = QPushButton("Pick")
        self.button.setFixedWidth(54)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)
        layout.addWidget(self.button)
        self.edit.editingFinished.connect(
            lambda: self.changed.emit(self.edit.text().strip())
        )
        self.button.clicked.connect(self._pick)

    def set_value(self, value: str) -> None:
        with QSignalBlocker(self.edit):
            self.edit.setText(value)

    def _pick(self) -> None:
        initial = QColor(self.edit.text())
        color = QColorDialog.getColor(
            initial if initial.isValid() else QColor("white"),
            self,
            "Choose colour",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if color.isValid():
            value = color.name(QColor.NameFormat.HexRgb)
            self.edit.setText(value)
            self.changed.emit(value)


class PropertyInspector(QWidget):
    """Property controls for the element selected in the embedded preview."""

    patch_changed = Signal(str, dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.element_id = ""
        self.element_type = ""
        self._loading = False

        self.title = QLabel("No element selected")
        self.title.setObjectName("InspectorTitle")
        self.details = QLabel(
            "Turn on Edit Mode, then point at and click an element in the preview."
        )
        self.details.setWordWrap(True)
        self.details.setObjectName("InspectorDetails")

        self.text = QLineEdit()
        self.source = QLineEdit()
        self.source.setPlaceholderText("Image URL or file path")

        self.font_size = self._spin(6, 200, " px")
        self.width = self._spin(0, 3000, " px")
        self.width.setSpecialValueText("Auto")
        self.height = self._spin(0, 3000, " px")
        self.height.setSpecialValueText("Auto")
        self.padding = self._spin(0, 300, " px")
        self.margin = self._spin(0, 300, " px")
        self.border_radius = self._spin(0, 300, " px")
        self.border_width = self._spin(0, 40, " px")

        self.text_color = ColorField()
        self.background_color = ColorField()
        self.border_color = ColorField()

        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(100)

        self.visible = QCheckBox("Visible")

        content_box = QGroupBox("Content")
        content_form = QFormLayout(content_box)
        content_form.addRow("Text", self.text)
        content_form.addRow("Image source", self.source)

        layout_box = QGroupBox("Layout")
        layout_form = QFormLayout(layout_box)
        layout_form.addRow("Width", self.width)
        layout_form.addRow("Height", self.height)
        layout_form.addRow("Padding", self.padding)
        layout_form.addRow("Margin", self.margin)

        appearance_box = QGroupBox("Appearance")
        appearance_form = QFormLayout(appearance_box)
        appearance_form.addRow("Font size", self.font_size)
        appearance_form.addRow("Text colour", self.text_color)
        appearance_form.addRow("Background", self.background_color)
        appearance_form.addRow("Border colour", self.border_color)
        appearance_form.addRow("Border width", self.border_width)
        appearance_form.addRow("Border radius", self.border_radius)
        appearance_form.addRow("Opacity", self.opacity)
        appearance_form.addRow("Visibility", self.visible)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.addWidget(self.title)
        content_layout.addWidget(self.details)
        content_layout.addSpacing(8)
        content_layout.addWidget(content_box)
        content_layout.addWidget(layout_box)
        content_layout.addWidget(appearance_box)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.text.editingFinished.connect(
            lambda: self._emit("text", self.text.text())
        )
        self.source.editingFinished.connect(
            lambda: self._emit("source", self.source.text().strip())
        )
        self.font_size.valueChanged.connect(
            lambda value: self._emit("font_size", value)
        )
        self.width.valueChanged.connect(lambda value: self._emit("width", value))
        self.height.valueChanged.connect(lambda value: self._emit("height", value))
        self.padding.valueChanged.connect(
            lambda value: self._emit("padding", value)
        )
        self.margin.valueChanged.connect(lambda value: self._emit("margin", value))
        self.text_color.changed.connect(lambda value: self._emit("color", value))
        self.background_color.changed.connect(
            lambda value: self._emit("background_color", value)
        )
        self.border_color.changed.connect(
            lambda value: self._emit("border_color", value)
        )
        self.border_width.valueChanged.connect(
            lambda value: self._emit("border_width", value)
        )
        self.border_radius.valueChanged.connect(
            lambda value: self._emit("border_radius", value)
        )
        self.opacity.valueChanged.connect(
            lambda value: self._emit("opacity", value / 100)
        )
        self.visible.toggled.connect(
            lambda value: self._emit("visible", value)
        )
        self.set_controls_enabled(False)

    @staticmethod
    def _spin(minimum: int, maximum: int, suffix: str = "") -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        if suffix:
            widget.setSuffix(suffix)
        return widget

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.text,
            self.source,
            self.font_size,
            self.width,
            self.height,
            self.text_color,
            self.background_color,
            self.border_color,
            self.border_width,
            self.border_radius,
            self.padding,
            self.margin,
            self.opacity,
            self.visible,
        ):
            widget.setEnabled(enabled)

    def set_selection(self, data: dict[str, Any]) -> None:
        self._loading = True
        try:
            self.element_id = str(data.get("id") or "")
            self.element_type = str(data.get("type") or "element")
            tag = str(data.get("tag") or "")
            computed = (
                data.get("computed")
                if isinstance(data.get("computed"), dict)
                else {}
            )

            friendly_name = str(data.get("label") or self.element_id or tag)
            self.title.setText(friendly_name or "Unnamed element")
            self.details.setText(
                f"Type: {self.element_type} · HTML: {tag}"
                + (f"\nSelector: {self.element_id}" if self.element_id.startswith("css|") else "")
            )
            self.text.setText(str(data.get("text") or ""))
            self.source.setText(str(data.get("source") or ""))
            self.font_size.setValue(self._number(computed.get("font_size"), 16))
            self.width.setValue(self._number(computed.get("width"), 0))
            self.height.setValue(self._number(computed.get("height"), 0))
            self.padding.setValue(self._number(computed.get("padding"), 0))
            self.margin.setValue(self._number(computed.get("margin"), 0))
            self.text_color.set_value(str(computed.get("color") or "#ffffffff"))
            self.background_color.set_value(
                str(computed.get("background_color") or "#00000000")
            )
            self.border_color.set_value(
                str(computed.get("border_color") or "#00000000")
            )
            self.border_width.setValue(
                self._number(computed.get("border_width"), 0)
            )
            self.border_radius.setValue(
                self._number(computed.get("border_radius"), 0)
            )
            opacity = float(computed.get("opacity") or 1)
            self.opacity.setValue(max(0, min(100, round(opacity * 100))))
            self.visible.setChecked(bool(computed.get("visible", True)))

            enabled = bool(self.element_id)
            self.set_controls_enabled(enabled)
            self.text.setEnabled(enabled and self.element_type in {"text", "button", "input"})
            self.source.setEnabled(enabled and self.element_type == "image")
        finally:
            self._loading = False

    @staticmethod
    def _number(value: Any, default: int) -> int:
        try:
            return max(0, int(round(float(value))))
        except (TypeError, ValueError):
            return default

    def clear_selection(self) -> None:
        self.element_id = ""
        self.element_type = ""
        self.title.setText("No element selected")
        self.details.setText(
            "Turn on Edit Mode, then point at and click an element in the preview."
        )
        self.set_controls_enabled(False)

    def _emit(self, key: str, value: Any) -> None:
        if self._loading or not self.element_id:
            return
        self.patch_changed.emit(self.element_id, {key: value})
