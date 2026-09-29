"""Small reusable widgets."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QStringListModel, Qt, Signal
from PySide6.QtWidgets import (QCompleter, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QToolButton,
                               QVBoxLayout, QWidget)


class TokenLineEdit(QLineEdit):
    """A line edit for a comma-separated list that completes the entry being typed.

    QCompleter is driven by hand here. Letting QLineEdit own the completer applies the completion
    twice for a multi-entry field, which used to turn one pick into "Jane Doe, Jane Doe, ".
    """

    def __init__(self, words=(), parent=None):
        super().__init__(parent)
        self._completer = QCompleter(list(words), self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchContains)
        self._completer.setCompletionMode(QCompleter.PopupCompletion)
        self._completer.setWidget(self)
        self._completer.activated.connect(self._accept)
        self.textEdited.connect(self._suggest)

    def set_words(self, words: list[str]) -> None:
        self._completer.setModel(QStringListModel(list(words), self._completer))

    def completer_popup(self):
        return self._completer.popup()

    def _typing(self) -> str:
        return self.text().split(",")[-1].strip()

    def _suggest(self) -> None:
        typed = self._typing()
        if not typed:
            self._completer.popup().hide()
            return
        self._completer.setCompletionPrefix(typed)
        if self._completer.completionCount():
            self._completer.complete()
        else:
            self._completer.popup().hide()

    def _accept(self, choice: str) -> None:
        kept = [part.strip() for part in self.text().split(",")[:-1] if part.strip()]
        if kept and kept[-1].casefold() == choice.casefold():
            kept.pop()  # nothing is added twice, whatever order Qt calls this in
        self.setText(", ".join(kept + [choice]) + ", ")
        self.setCursorPosition(len(self.text()))

    def entries(self) -> list[str]:
        return [part.strip() for part in self.text().split(",") if part.strip()]


class StarRating(QWidget):
    changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.value = 0
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.buttons = []
        for i in range(5):
            b = QToolButton(self)
            b.setToolTip(f"{i + 1} star{'s' if i else ''} (click again to clear)")
            b.clicked.connect(lambda _=False, n=i + 1: self._clicked(n))
            b.setStyleSheet("QToolButton { font-size: 18px; color: #f5c451; padding: 0 2px; }")
            row.addWidget(b)
            self.buttons.append(b)
        self.set_value(0)

    def _clicked(self, n: int) -> None:
        self.set_value(0 if n == self.value else n)
        self.changed.emit(self.value)

    def set_value(self, value: int) -> None:
        self.value = value
        for i, b in enumerate(self.buttons):
            b.setText("★" if i < value else "☆")


def ask_text(parent, title: str, label: str, words=(), text: str = "", multi: bool = False) -> str | None:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    prompt = QLabel(label)
    prompt.setWordWrap(True)
    layout.addWidget(prompt)
    if multi:
        edit = TokenLineEdit(list(words), dialog)
        edit.setText(text)
    else:
        edit = QLineEdit(text, dialog)
        completer = QCompleter(list(words), edit)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        edit.setCompleter(completer)
    edit.selectAll()
    layout.addWidget(edit)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    dialog.resize(440, dialog.sizeHint().height())
    if dialog.exec() != QDialog.Accepted:
        return None
    return edit.text().strip().strip(",").strip() or None

class NoStrayScrolling(QObject):
    """Stops a spin box or dropdown changing when the window is scrolled past it.

    Qt sends wheel events to whatever the pointer is over, focused or not, so scrolling a
    settings panel silently rewrites the settings you scrolled past. That is not a theoretical
    risk: five notches over a lens angle turned 180 into 155 and pointed a half-finished job
    at an empty folder, which looked like every finished chunk had vanished.

    A control that has been clicked into still scrolls, because then it was meant.
    """

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Wheel and not watched.hasFocus():
            event.ignore()
            return True
        return super().eventFilter(watched, event)


def no_stray_scrolling(*widgets) -> NoStrayScrolling:
    """Apply that to some controls, and hand back the filter so it stays alive."""
    guard = NoStrayScrolling(widgets[0] if widgets else None)
    for widget in widgets:
        widget.setFocusPolicy(Qt.StrongFocus)
        widget.installEventFilter(guard)
    return guard
