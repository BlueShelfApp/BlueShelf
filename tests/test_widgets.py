import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from blueshelf.widgets import TokenLineEdit


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def edit(qt_app):
    widget = TokenLineEdit(["Jane Doe", "Jane Doherty", "Anna Bell"])
    widget.show()
    yield widget
    widget.close()


def pick(edit, typed, existing="", how="mouse"):
    """Type into the field and choose the first suggestion the way a person would."""
    edit.setText(existing)
    edit.setCursorPosition(len(existing))
    edit.setFocus()
    QTest.keyClicks(edit, typed)
    QApplication.processEvents()
    popup = edit.completer_popup()
    assert popup.isVisible(), "the suggestion list should be showing"
    index = popup.model().index(0, 0)
    popup.setCurrentIndex(index)
    if how == "mouse":
        QTest.mouseClick(popup.viewport(), Qt.LeftButton, Qt.NoModifier, popup.visualRect(index).center())
    else:
        QTest.keyClick(popup, Qt.Key_Return)
    QApplication.processEvents()
    return edit.text()


def test_clicking_a_suggestion_adds_the_name_once(edit):
    assert pick(edit, "Jane") == "Jane Doe, "


def test_pressing_enter_adds_the_name_once(edit):
    assert pick(edit, "Jane", how="enter") == "Jane Doe, "


def test_a_second_name_keeps_the_first(edit):
    assert pick(edit, "Jane", existing="Anna Bell, ") == "Anna Bell, Jane Doe, "


def test_the_list_is_read_back_without_blanks(edit):
    edit.setText("Anna Bell, Jane Doe, ")
    assert edit.entries() == ["Anna Bell", "Jane Doe"]
    edit.setText("  ")
    assert edit.entries() == []


def test_words_can_be_replaced_later(edit):
    edit.set_words(["Cara Mell"])
    assert pick(edit, "Cara") == "Cara Mell, "
