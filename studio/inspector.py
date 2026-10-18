from __future__ import annotations

import math
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
        self.edit.editingFinished.connect(self._editing_finished)
        self.button.clicked.connect(self._pick)

    def set_value(self, value: str) -> None:
        with QSignalBlocker(self.edit):
            self.edit.setText(value)
            self.edit.setCursorPosition(0)

    def _editing_finished(self) -> None:
        if self.isEnabled() and not self.edit.isReadOnly():
            self.changed.emit(self.edit.text().strip())

    def _pick(self) -> None:
        if not self.isEnabled() or self.edit.isReadOnly():
            return
        initial = QColor(self.edit.text())
        color = QColorDialog.getColor(
            initial if initial.isValid() else QColor("white"),
            self,
            "Choose colour",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if color.isValid():
            value = color.name(QColor.NameFormat.HexRgb)
            if color.alpha() < 255:
                value = f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alphaF():.3f})"
            self.edit.setText(value)
            self.changed.emit(value)


class PropertyInspector(QWidget):
    """Property controls and project-level Save for the visual editor.

    Text edits remain local until they are committed. The footer includes those
    pending edits immediately, so Save is usable while the caret is still in a
    field. Selection changes commit to the previous element before replacing
    its identity; callers must also commit before changing project ownership.
    """

    patch_changed = Signal(str, dict)
    save_requested = Signal()
    pending_edits_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.element_id = ""
        self.element_type = ""
        self._loading = False
        self._committing = False
        self._allowed_fields: set[str] = set()
        self._supported_fields: set[str] = set()
        self._field_reasons: dict[str, str] = {}
        self._last_values: dict[str, Any] = {}
        self._last_spin_text: dict[str, str] = {}
        self._pending_state = False
        self._saved_dirty = False
        self._save_enabled = False
        self._save_project_name = ""

        self.heading = QLabel("Properties")
        self.heading.setObjectName("PropertiesHeading")
        self.heading.setContentsMargins(12, 12, 12, 8)
        self.title = QLabel("No element selected")
        self.title.setObjectName("InspectorTitle")
        self.details = QLabel(
            "Turn on Edit Mode, then click an element in the preview. "
            "Only settings you can edit are shown."
        )
        self.details.setWordWrap(True)
        self.details.setObjectName("InspectorDetails")
        self.inline_hint = QLabel(
            "Click text in the preview to edit it. Enter to finish; Esc to cancel."
        )
        self.inline_hint.setObjectName("PropertiesInlineHint")
        self.inline_hint.setWordWrap(True)

        self.text = QLineEdit()
        # Preview text is not limited to QLineEdit's default 32,767 characters.
        # A truncated mirror would look like an unfinished Properties edit and
        # could overwrite the complete browser draft when Save flushes fields.
        self.text.setMaxLength(2_147_483_647)
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
        self.visible = QCheckBox("Visible")

        self._fields: dict[str, QWidget] = {
            "text": self.text,
            "source": self.source,
            "font_size": self.font_size,
            "width": self.width,
            "height": self.height,
            "padding": self.padding,
            "margin": self.margin,
            "color": self.text_color,
            "background_color": self.background_color,
            "border_color": self.border_color,
            "border_width": self.border_width,
            "border_radius": self.border_radius,
            "opacity": self.opacity,
            "visible": self.visible,
        }
        self.field_rows: dict[str, QWidget] = {}
        self.field_labels: dict[str, QWidget] = {}
        self.field_markers: dict[str, QLabel] = {}
        self._field_forms: dict[str, QFormLayout] = {}
        self._group_fields = {
            "content": ("text", "source"),
            "layout": ("width", "height", "padding", "margin"),
            "appearance": (
                "font_size", "color", "background_color", "border_color",
                "border_width", "border_radius", "opacity", "visible",
            ),
        }

        content_box = QGroupBox("Content")
        content_form = QFormLayout(content_box)
        self._add_field(content_form, "Text", "text")
        self._add_field(content_form, "Image source", "source")

        layout_box = QGroupBox("Layout")
        layout_form = QFormLayout(layout_box)
        for label, key in (
            ("Width", "width"), ("Height", "height"),
            ("Padding", "padding"), ("Margin", "margin"),
        ):
            self._add_field(layout_form, label, key)

        appearance_box = QGroupBox("Appearance")
        appearance_form = QFormLayout(appearance_box)
        for label, key in (
            ("Font size", "font_size"),
            ("Text colour", "color"),
            ("Background", "background_color"),
            ("Border colour", "border_color"),
            ("Border width", "border_width"),
            ("Border radius", "border_radius"),
            ("Opacity", "opacity"),
            ("Visibility", "visible"),
        ):
            self._add_field(appearance_form, label, key)
        self.groups = {
            "content": content_box,
            "layout": layout_box,
            "appearance": appearance_box,
        }

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.addWidget(self.title)
        content_layout.addWidget(self.details)
        content_layout.addWidget(self.inline_hint)
        content_layout.addSpacing(8)
        content_layout.addWidget(content_box)
        content_layout.addWidget(layout_box)
        content_layout.addWidget(appearance_box)
        content_layout.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(content)

        self.save_footer = QFrame()
        self.save_footer.setObjectName("InspectorSaveFooter")
        self.save_status = QLabel("No project selected")
        self.save_status.setObjectName("InspectorSaveStatus")
        self.save_status.setWordWrap(True)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("InspectorSaveButton")
        self.save_button.setMinimumWidth(80)
        self.save_button.clicked.connect(self._request_save)
        footer_layout = QHBoxLayout(self.save_footer)
        footer_layout.setContentsMargins(12, 10, 12, 10)
        footer_layout.addWidget(self.save_status, 1)
        footer_layout.addWidget(self.save_button)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.heading)
        outer.addWidget(self.scroll, 1)
        outer.addWidget(self.save_footer)

        self.setStyleSheet("""
            #PropertiesHeading { color: #ffffff; font-size: 18px; font-weight: 800; }
            #InspectorTitle { font-size: 15px; }
            #PropertiesInlineHint { color: #a9bbd6; }
            #InspectorSaveFooter { background: #111a2d; border-top: 1px solid #304769; }
            #InspectorSaveButton { background: #264f82; border-color: #527cad; font-weight: 700; }
            #InspectorSaveButton:hover { background: #326499; }
            #InspectorSaveButton:disabled { background: #162139; color: #788ba9; border-color: #2b3b55; }
        """)

        for key, widget in self._fields.items():
            if isinstance(widget, QLineEdit):
                widget.textEdited.connect(self._pending_edited)
                widget.editingFinished.connect(lambda field=key: self._commit_field(field))
            elif isinstance(widget, QSpinBox):
                widget.lineEdit().textEdited.connect(self._pending_edited)
                widget.valueChanged.connect(lambda value, field=key: self._emit(field, value))
                widget.editingFinished.connect(self._pending_edited)
            elif isinstance(widget, ColorField):
                widget.edit.textEdited.connect(self._pending_edited)
                widget.changed.connect(lambda value, field=key: self._emit(field, value))
        self.opacity.valueChanged.connect(lambda value: self._emit("opacity", value / 100))
        self.visible.toggled.connect(lambda value: self._emit("visible", value))
        self.clear_selection()
        self._update_save_footer()

    def _add_field(self, form: QFormLayout, label: str, key: str) -> None:
        widget = self._fields[key]
        widget.setObjectName(f"InspectorField_{key}")
        widget.setAccessibleName(label)
        row = QWidget()
        row.setObjectName(f"InspectorRow_{key}")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(widget, 1)
        # Retain this object for integrations from earlier Studio releases. The
        # current panel hides unavailable rows instead of displaying markers.
        marker = QLabel("")
        marker.setObjectName(f"InspectorUnavailable_{key}")
        marker.setFixedWidth(18)
        marker.setAlignment(Qt.AlignmentFlag.AlignCenter)
        marker.setStyleSheet(
            "color: #aebcce; font-size: 20px; font-weight: 700; background: transparent;"
        )
        layout.addWidget(marker)
        self.field_rows[key] = row
        self.field_markers[key] = marker
        form.addRow(label, row)
        self.field_labels[key] = form.labelForField(row)
        self._field_forms[key] = form
        marker.hide()

    @staticmethod
    def _spin(minimum: int, maximum: int, suffix: str = "") -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setKeyboardTracking(False)
        if suffix:
            widget.setSuffix(suffix)
        return widget

    @property
    def has_pending_edits(self) -> bool:
        if self._loading or not self.element_id:
            return False
        for key in self._allowed_fields:
            widget = self._fields[key]
            if self._field_value(key) != self._last_values.get(key):
                return True
            if isinstance(widget, QSpinBox) and widget.text() != self._last_spin_text.get(key):
                return True
        return False

    def set_save_state(self, dirty: bool, *, project_name: str = "", enabled: bool = True) -> None:
        self._saved_dirty = bool(dirty)
        self._save_project_name = project_name
        self._save_enabled = bool(enabled)
        self._update_save_footer()

    def _update_save_footer(self) -> None:
        dirty = self._saved_dirty or self.has_pending_edits
        self.save_button.setEnabled(self._save_enabled and dirty)
        if not self._save_enabled:
            status = "No project selected"
            tooltip = "Select a project to save visual changes."
        elif dirty:
            status = "Unsaved changes"
            tooltip = "Save visual changes for this project."
        else:
            status = "Saved"
            tooltip = "All visual changes for this project are saved."
        if self._save_project_name:
            tooltip = f"{self._save_project_name}\n{tooltip}"
        self.save_status.setText(status)
        self.save_status.setAccessibleDescription(tooltip)
        self.save_status.setToolTip(tooltip)
        self.save_status.setStyleSheet(
            f"color: {'#f1c47a' if dirty else '#a9bbd6'}; border: 0; background: transparent;"
        )
        self.save_button.setToolTip(tooltip)
        self.save_button.setAccessibleDescription(tooltip)

    def _pending_edited(self, *_args) -> None:
        if self._loading:
            return
        pending = self.has_pending_edits
        self._update_save_footer()
        if pending != self._pending_state:
            self._pending_state = pending
            self.pending_edits_changed.emit(pending)

    def _request_save(self) -> None:
        if not self._save_enabled:
            return
        self.commit_pending_edits()
        self.save_requested.emit()

    def _set_field_available(self, key: str, allowed: bool, reason: str = "") -> None:
        widget = self._fields[key]
        row = self.field_rows[key]
        marker = self.field_markers[key]
        widget.setEnabled(allowed)
        if isinstance(widget, (QLineEdit, QSpinBox)):
            widget.setReadOnly(not allowed)
        elif isinstance(widget, ColorField):
            widget.edit.setReadOnly(not allowed)
            widget.edit.setEnabled(allowed)
            widget.button.setEnabled(allowed)
        description = "Editable" if allowed else reason
        widget.setAccessibleDescription(description)
        for part in [row, widget, *widget.findChildren(QWidget), marker]:
            part.setToolTip("" if allowed else reason)
            part.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
            if allowed:
                part.unsetCursor()
            else:
                part.setCursor(Qt.CursorShape.ForbiddenCursor)
        self._field_forms[key].setRowVisible(row, allowed)
        marker.setText("")
        marker.hide()
        marker.setAccessibleName("" if allowed else f"Unavailable: {widget.accessibleName()}")
        marker.setAccessibleDescription("" if allowed else reason)
        widget.setStyleSheet(
            "" if allowed else "color: #74849f; background: #10192a; border-color: #22314a;"
        )

    def set_controls_enabled(self, enabled: bool) -> None:
        self._allowed_fields = self._supported_fields.copy() if enabled and self.element_id else set()
        for key in self._fields:
            allowed = key in self._allowed_fields
            reason = self._field_reasons.get(key, "This setting does not apply to the selected element.")
            if not enabled or not self.element_id:
                reason = "Select an element in Edit Mode to change this setting."
            self._set_field_available(key, allowed, reason)
        for name, group in self.groups.items():
            group.setVisible(any(key in self._allowed_fields for key in self._group_fields[name]))
        self.inline_hint.setVisible("text" in self._allowed_fields)

    def _selection_capabilities(self, data: dict[str, Any]) -> None:
        fields = data.get("supported_fields")
        reasons = data.get("field_reasons")
        self._field_reasons = {
            str(key): str(value)
            for key, value in (reasons.items() if isinstance(reasons, dict) else [])
        }
        if isinstance(fields, list):
            self._supported_fields = {
                key for key in fields if isinstance(key, str) and key in self._fields
            }
            return
        # Older editor payloads do not describe the DOM's exact capabilities.
        self._supported_fields = set(self._fields)
        tag = str(data.get("tag") or "").lower()
        if self.element_type not in {"text", "button"} or tag in {"input", "textarea", "select"}:
            self._supported_fields.discard("text")
            self._field_reasons["text"] = (
                "Select a text label to change its text. Live input values and containers are not text labels."
            )
        if tag != "img":
            self._supported_fields.discard("source")
            self._field_reasons["source"] = "Image source is available only for an image element."
        if self.element_type == "image" or tag in {
            "img", "picture", "svg", "canvas", "video", "audio", "iframe", "object", "embed",
        }:
            for key in ("font_size", "color"):
                self._supported_fields.discard(key)
                self._field_reasons[key] = "This element does not display editable text."

    def set_selection(self, data: dict[str, Any]) -> None:
        self.commit_pending_edits()
        if not data.get("id"):
            self.clear_selection()
            return
        self._loading = True
        try:
            self.element_id = str(data.get("id") or "")
            self.element_type = str(data.get("type") or "element")
            tag = str(data.get("tag") or "")
            computed = data.get("computed") if isinstance(data.get("computed"), dict) else {}
            friendly_name = str(data.get("label") or self.element_id or tag)
            self.title.setText(friendly_name or "Unnamed element")
            self.details.setText(
                f"Type: {self.element_type} · HTML: {tag}"
                + (f"\nSelector: {self.element_id}" if self.element_id.startswith("css|") else "")
            )
            self.text.setText(str(data.get("text") or ""))
            self.source.setText(str(data.get("source") or ""))
            self.text.setCursorPosition(0)
            self.source.setCursorPosition(0)
            for key in (
                "font_size", "width", "height", "padding",
                "margin", "border_width", "border_radius",
            ):
                self._fields[key].setSpecialValueText("Auto" if key in {"width", "height"} else "")
                self._fields[key].setValue(
                    self._number(computed.get(key), 16 if key == "font_size" else 0)
                )
            self.text_color.set_value(str(computed.get("color") or "#ffffff"))
            self.background_color.set_value(str(computed.get("background_color") or "transparent"))
            self.border_color.set_value(str(computed.get("border_color") or "transparent"))
            try:
                opacity = float(computed.get("opacity", 1))
                if not math.isfinite(opacity):
                    opacity = 1
            except (TypeError, ValueError):
                opacity = 1
            self.opacity.setValue(max(0, min(100, round(opacity * 100))))
            self.visible.setChecked(bool(computed.get("visible", True)))
            self._selection_capabilities(data)
            self.set_controls_enabled(True)
            if not self._allowed_fields:
                self.details.setText(
                    self.details.text() + "\nThis element has no editable properties."
                )
            self._remember_values()
        finally:
            self._loading = False
        self._pending_edited()

    def update_inline_text(self, element_id: str, text: str) -> bool:
        """Reflect a preview text edit without emitting it again or committing fields.

        Browser input can arrive while another property still has an unfinished
        value. Only the selected element's Text field and its baseline change;
        all other field values, focus and pending edits are preserved. If Text
        itself has an unfinished panel edit, that local input takes precedence.
        """
        if (
            self._loading
            or not element_id
            or element_id != self.element_id
            or "text" not in self._allowed_fields
            or self.text.text() != self._last_values.get("text")
        ):
            return False
        value = str(text)
        if self.text.text() != value:
            previous = self.text.text()
            cursor = self.text.cursorPosition()
            with QSignalBlocker(self.text):
                self.text.setText(value)
                if self.text.text() != value:
                    # Retain the previous mirror if a native control cannot
                    # represent this value; never create a synthetic edit.
                    self.text.setText(previous)
                    self.text.setCursorPosition(cursor)
                    return False
                self.text.setCursorPosition(0)
        self._last_values["text"] = value
        self._pending_edited()
        return True

    @staticmethod
    def _number(value: Any, default: int) -> int:
        try:
            return max(0, int(round(float(value))))
        except (TypeError, ValueError, OverflowError):
            return default

    def clear_selection(self) -> None:
        self.commit_pending_edits()
        self._loading = True
        try:
            self.element_id = ""
            self.element_type = ""
            self.title.setText("No element selected")
            self.details.setText(
                "Turn on Edit Mode, then click an element in the preview. "
                "Only settings you can edit are shown."
            )
            self._supported_fields.clear()
            self._field_reasons.clear()
            self.set_controls_enabled(False)
            self.text.clear()
            self.source.clear()
            for widget in self._fields.values():
                if isinstance(widget, QSpinBox):
                    widget.setSpecialValueText("—")
                    widget.setValue(widget.minimum())
                elif isinstance(widget, ColorField):
                    widget.set_value("")
            self.opacity.setValue(0)
            self.visible.setChecked(False)
            self._remember_values()
        finally:
            self._loading = False
        self._pending_edited()

    def _field_value(self, key: str) -> Any:
        widget = self._fields[key]
        if isinstance(widget, QLineEdit):
            return widget.text() if key == "text" else widget.text().strip()
        if isinstance(widget, ColorField):
            return widget.edit.text().strip()
        if isinstance(widget, QSpinBox):
            return widget.value()
        if key == "opacity":
            return self.opacity.value() / 100
        return self.visible.isChecked()

    def _remember_values(self) -> None:
        self._last_values = {key: self._field_value(key) for key in self._fields}
        self._last_spin_text = {
            key: widget.text()
            for key, widget in self._fields.items()
            if isinstance(widget, QSpinBox)
        }

    def _commit_field(self, key: str) -> None:
        self._emit(key, self._field_value(key))

    def commit_pending_edits(self) -> None:
        """Flush focused text/spin editors to the currently selected element."""
        if self._loading or self._committing or not self.element_id:
            return
        self._committing = True
        try:
            for key, widget in self._fields.items():
                if key not in self._allowed_fields:
                    continue
                if isinstance(widget, QSpinBox):
                    widget.interpretText()
                    self._last_spin_text[key] = widget.text()
                self._commit_field(key)
        finally:
            self._committing = False
        self._pending_edited()

    def _emit(self, key: str, value: Any) -> None:
        if self._loading or not self.element_id or key not in self._allowed_fields:
            return
        widget = self._fields[key]
        if not widget.isEnabled():
            return
        if isinstance(widget, QSpinBox):
            self._last_spin_text[key] = widget.text()
        if value != self._last_values.get(key):
            self._last_values[key] = value
            self.patch_changed.emit(self.element_id, {key: value})
        self._pending_edited()
