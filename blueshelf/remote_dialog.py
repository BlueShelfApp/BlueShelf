"""Switch on the page that a Quest headset or phone opens to browse, watch and delete."""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton,
                               QSpinBox, QVBoxLayout)

from blueshelf import dlna, firewall
from blueshelf.db import Database
from blueshelf.server import DEFAULT_PORT, RESERVED_WORDS, RemoteServer, make_token


class RemoteDialog(QDialog):
    def __init__(self, db: Database, server: RemoteServer, parent=None,
                 media_server: dlna.DlnaServer | None = None):
        super().__init__(parent)
        self.db = db
        self.server = server
        self._waiting_for_rule = 0
        self.setWindowTitle("Share with a headset, phone or TV")
        self.resize(780, 780)

        intro = QLabel(
            "Open the address below in the Quest browser and your library appears there: browse by performer, "
            "watch in VR, and delete the video you are watching. Deleted files go to <b>BlueShelf's trash on this "
            "PC</b>, so a mistaken tap can still be undone here.<br><br>"
            "The address only answers while the switch below is on and BlueShelf is running, and only devices on "
            "your own network can reach it. The last part of the address is the passcode: change it to any word "
            "you like, so it is easy to type in a headset.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)

        self.enable_check = QCheckBox("Serve my library on this network")
        self.enable_check.setChecked(bool(db.get_setting("remote_enabled", False)))
        self.enable_check.toggled.connect(self._toggle)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(db.get_setting("remote_port", DEFAULT_PORT)))
        self.port_spin.editingFinished.connect(self._port_changed)
        self.word_edit = QLineEdit(db.get_setting("remote_token", ""))
        self.word_edit.setMaximumWidth(220)
        self.word_edit.setToolTip("This word is the address and the passcode in one. Anyone on your network "
                                  "who guesses it can open your library, so avoid something obvious if that "
                                  "matters to you.")
        self.word_edit.editingFinished.connect(self._word_changed)
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port"))
        port_row.addWidget(self.port_spin)
        port_row.addSpacing(18)
        port_row.addWidget(QLabel("Address word"))
        port_row.addWidget(self.word_edit)
        port_row.addStretch()

        self.address_label = QLabel()
        self.address_label.setTextFormat(Qt.RichText)
        self.address_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.address_label.setWordWrap(True)

        self.firewall_label = QLabel()
        self.firewall_label.setWordWrap(True)
        self.firewall_label.setTextFormat(Qt.RichText)
        self.firewall_btn = QPushButton("Allow through Windows Firewall")
        self.firewall_btn.setToolTip("Adds a rule for this port, limited to your own network. Windows will ask "
                                     "for permission.")
        self.firewall_btn.clicked.connect(self._allow_firewall)
        firewall_row = QHBoxLayout()
        firewall_row.addWidget(self.firewall_label, 1)
        firewall_row.addWidget(self.firewall_btn)

        self.plays_label = QLabel()
        self.plays_label.setWordWrap(True)
        self.plays_label.setObjectName("muted")
        self._refresh_plays()

        copy_btn = QPushButton("Copy address")
        copy_btn.clicked.connect(self._copy)
        new_code = QPushButton("New passcode")
        new_code.setToolTip("Stops any device that has the old address from getting in")
        new_code.clicked.connect(self._new_token)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(copy_btn)
        buttons.addWidget(new_code)
        buttons.addStretch()
        buttons.addWidget(close)

        note = QLabel(
            "<b>The first time, the headset will warn that the page is not private.</b> Choose <i>Advanced</i> "
            "and then <i>Continue</i>. BlueShelf makes its own certificate for this PC, which no browser has heard "
            "of, and browsers only hand out VR on a secure page - so this warning is the price of VR, and it "
            "only appears once per device.<br><br>"
            "On the headset: open the browser, type the address, then tap a video. A VR video appears as a "
            "window you can drag; tap <b>VR</b> to put it around you. In VR, pull the trigger to bring up the "
            "controls - play, skip, favourite and delete - and point at them. Delete asks twice, so a stray "
            "press cannot remove a file.<br><br>"
            "If a device cannot reach it, check that it is on the same Wi-Fi, and that the Wi-Fi is not a "
            "guest network, which usually blocks devices from seeing each other.")
        note.setWordWrap(True)
        note.setObjectName("muted")

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addSpacing(8)
        layout.addWidget(self.enable_check)
        layout.addLayout(port_row)
        layout.addWidget(self.address_label)
        layout.addSpacing(6)
        layout.addLayout(firewall_row)
        layout.addWidget(self.plays_label)
        layout.addWidget(note)
        if media_server is not None:
            layout.addSpacing(10)
            layout.addWidget(DlnaBox(db, media_server, self))
        layout.addStretch()
        layout.addLayout(buttons)
        self._refresh()

    def _refresh_plays(self) -> None:
        """How much has actually been watched away from this PC, and where."""
        names = {"deovr": "DeoVR", "page": "this page", "dlna": "DLNA players"}
        counts = {source: n for source, n in self.db.play_counts().items() if source in names}
        if not counts:
            self.plays_label.setText("Nothing has been played away from this PC yet.")
            return
        parts = ", ".join(f"<b>{n}</b> through {names[source]}" for source, n in sorted(counts.items()))
        self.plays_label.setText(f"Played away from this PC: {parts}.")

    def addresses(self) -> list[str]:
        word = self.db.get_setting("remote_token", "")
        return [f"{base}/{word}" for base in self.server.addresses()]

    def _word_changed(self) -> None:
        word = self.word_edit.text().strip().strip("/")
        if word == self.db.get_setting("remote_token", ""):
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", word):
            QMessageBox.warning(self, "Pick another word",
                                "Use 3 to 40 letters, digits, dashes or underscores, with no spaces. "
                                "It becomes part of the address, so keep it simple to type in a headset.")
            self.word_edit.setText(self.db.get_setting("remote_token", ""))
            return
        if word.lower() in RESERVED_WORDS:
            QMessageBox.warning(self, "Pick another word",
                                f"“{word}” is used by the page itself, so the library would not "
                                "load. Pick a different word.")
            self.word_edit.setText(self.db.get_setting("remote_token", ""))
            return
        self.db.set_setting("remote_token", word)
        self.server.token = word
        if self.server.running:
            self.server.stop()
            self._toggle(True)
        self._refresh()

    def _refresh(self) -> None:
        if not self.server.running:
            self.address_label.setText("<span style='color:#8d8f98'>Switched off. Turn it on to get an address.</span>")
        else:
            addresses = self.addresses()
            word = self.db.get_setting("remote_token", "")
            # DeoVR refuses a certificate this PC signed for itself, so its address is the plain one.
            plain = addresses[0].replace("https://", "http://", 1)
            deovr_address = f"{plain}/deovr"
            without_word = plain.rsplit("/", 1)[0] + "/deovr" if word else deovr_address
            text = (f"Open this in the headset browser:<br><br><b style='font-size:15px'>{addresses[0]}</b>"
                    f"<br><br>Or type this into <b>DeoVR</b>, which browses the library itself - by performer, "
                    f"with every video's VR shape already set:<br><b style='font-size:15px'>"
                    f"{deovr_address}</b><br><span style='color:#8d8f98'>Note <b>http</b>, not https: DeoVR "
                    f"will not take the certificate this PC signs for itself.<br>If DeoVR shows the raw text "
                    f"rather than your library, give it <b>{without_word}</b> instead and the word "
                    f"<b>{word}</b> as the password - newer versions ask the address for its own feed and "
                    f"ignore the rest of what you typed.</span>")
            if len(addresses) > 1:
                others = "<br>".join(addresses[1:])
                text += ("<br><br><span style='color:#8d8f98'>This PC also answers on the addresses below, but "
                         f"they usually belong to virtual adapters a headset cannot reach:<br>{others}</span>")
            self.address_label.setText(text)
        self._refresh_firewall()

    def _refresh_firewall(self) -> None:
        if not firewall.supported():
            self.firewall_label.setText("")
            self.firewall_btn.hide()
            return
        port = self.port_spin.value()
        allowed = firewall.rule_exists(port)
        self.firewall_btn.setEnabled(not allowed)
        self.firewall_btn.setVisible(not allowed)
        if allowed:
            self.firewall_label.setText(f"Windows Firewall allows port {port} from your own network.")
            return
        extra = ""
        if firewall.network_is_public():
            extra = (" Your Wi-Fi is set to <b>Public</b>, where Windows blocks incoming connections hardest, so "
                     "this step is needed.")
        self.firewall_label.setText(
            f"<b>Windows Firewall is blocking port {port}.</b> Other devices will fail to connect until this is "
            f"allowed.{extra}")

    def _toggle(self, on: bool) -> None:
        self.db.set_setting("remote_enabled", on)
        try:
            if on:
                if not self.db.get_setting("remote_token", ""):
                    self.db.set_setting("remote_token", make_token())
                self.server.token = self.db.get_setting("remote_token", "")
                self.server.port = self.port_spin.value()
                self.server.start()
            else:
                self.server.stop()
        except OSError as exc:
            self.enable_check.setChecked(False)
            QMessageBox.warning(self, "Could not start",
                                f"{exc.strerror or exc}\n\nAnother program may be using port "
                                f"{self.port_spin.value()}. Try a different port.")
        self._refresh()

    def _port_changed(self) -> None:
        if self.port_spin.value() == int(self.db.get_setting("remote_port", DEFAULT_PORT)):
            return
        self.db.set_setting("remote_port", self.port_spin.value())
        if self.server.running:
            self.server.stop()
            self._toggle(True)
        else:
            self._refresh_firewall()

    def _allow_firewall(self) -> None:
        port = self.port_spin.value()
        if not firewall.request_rule(port):
            QMessageBox.information(self, "Not allowed",
                                    "Windows did not add the rule. It needs the permission prompt to be "
                                    "accepted. You can also add it by hand: allow incoming TCP on port "
                                    f"{port} for private networks.")
            return
        self.firewall_btn.setEnabled(False)
        self.firewall_label.setText("Waiting for Windows to add the rule…")
        self._waiting_for_rule = 0
        QTimer.singleShot(700, self._poll_rule)

    def _poll_rule(self) -> None:
        self._waiting_for_rule += 1
        if firewall.rule_exists(self.port_spin.value()) or self._waiting_for_rule > 20:
            self._refresh_firewall()
            return
        QTimer.singleShot(700, self._poll_rule)

    def _copy(self) -> None:
        addresses = self.addresses()
        if addresses:
            QGuiApplication.clipboard().setText(addresses[0])

    def _new_token(self) -> None:
        self.db.set_setting("remote_token", make_token())
        self.server.token = self.db.get_setting("remote_token", "")
        self.word_edit.setText(self.server.token)
        if self.server.running:
            self.server.stop()
            self._toggle(True)
        self._refresh()


class DlnaBox(QGroupBox):
    """Sharing with DeoVR, Skybox, TVs and consoles, which find the library by DLNA."""

    def __init__(self, db: Database, server: dlna.DlnaServer, parent=None):
        super().__init__("DeoVR, Skybox and TVs (DLNA)", parent)
        self.db = db
        self.server = server
        self._waiting = 0

        self.check = QCheckBox("Share my library with DLNA players on this network")
        self.check.setChecked(server.running)
        self.check.toggled.connect(self._toggle)

        self.name_edit = QLineEdit(server.name)
        self.name_edit.setMaximumWidth(320)
        self.name_edit.setToolTip("What players list this library as")
        self.name_edit.editingFinished.connect(self._rename)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Show it as"))
        name_row.addWidget(self.name_edit)
        name_row.addStretch()

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.RichText)

        self.firewall_label = QLabel()
        self.firewall_label.setWordWrap(True)
        self.firewall_label.setTextFormat(Qt.RichText)
        self.firewall_btn = QPushButton("Allow through Windows Firewall")
        self.firewall_btn.setToolTip("Adds two rules - one for the videos, one for being found - limited to "
                                     "your own network. Windows will ask for permission.")
        self.firewall_btn.clicked.connect(self._allow)
        firewall_row = QHBoxLayout()
        firewall_row.addWidget(self.firewall_label, 1)
        firewall_row.addWidget(self.firewall_btn)

        note = QLabel(
            "In DeoVR or Skybox, open the media server (DLNA) list and pick this library. It is laid "
            "out like the sidebar here - performers, studios, categories, VR - and VR scenes open in the "
            "right projection by themselves.<br><br>"
            "<b>DLNA has no passcode.</b> Anything on your network that speaks it can browse and play "
            "what is shared. Videos you have hidden are never shared, and nothing outside your own "
            "network can reach it.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)
        note.setObjectName("muted")

        layout = QVBoxLayout(self)
        layout.addWidget(self.check)
        layout.addLayout(name_row)
        layout.addWidget(self.status)
        layout.addLayout(firewall_row)
        layout.addWidget(note)
        self._refresh()
        # The search count moves while the window is open, which is when someone is trying a player.
        self._live = QTimer(self, interval=2000)
        self._live.timeout.connect(self._refresh_status)
        self._live.start()

    def _toggle(self, on: bool) -> None:
        self.db.set_setting("dlna_enabled", on)
        if on:
            problem = dlna.start_saved(self.server, self.db)
            if problem:
                self.check.blockSignals(True)
                self.check.setChecked(False)
                self.check.blockSignals(False)
                self.db.set_setting("dlna_enabled", False)
                QMessageBox.warning(self, "Could not start",
                                    f"{problem}\n\nAnother program may be using the port.")
        else:
            self.server.stop()
        self._refresh()

    def _rename(self) -> None:
        name = self.name_edit.text().strip()
        if not name or name == self.server.name:
            self.name_edit.setText(self.server.name)
            return
        self.db.set_setting("dlna_name", name)
        self.server.name = name
        if self.server.running:  # players read the name when they find the server, so announce again
            self.server.stop()
            dlna.start_saved(self.server, self.db)
        self._refresh()

    def _refresh(self) -> None:
        self._refresh_status()
        self._refresh_firewall()

    def _refresh_status(self) -> None:
        if not self.server.running:
            self.status.setText("<span style='color:#8d8f98'>Switched off.</span>")
            return
        if not self.server.discoverable:
            self.status.setText(f"Sharing on port {self.server.port}, but players will not find it on their "
                                "own: another program is holding the discovery port. Restarting BlueShelf "
                                "usually clears it.")
            return
        s = self.server.searches()
        text = f"Sharing as <b>{self.server.name}</b> on port {self.server.port}."
        if s["asked_remote"]:
            text += (f"<br>Other devices have looked for media servers {s['asked_remote']} times; BlueShelf "
                     f"answered {s['answered_remote']}. Last asked by {s['last_asked']}")
            text += f", last answered {s['last_answered']}." if s["last_answered"] else "."
        elif s["heard"]:
            text += ("<br>Searches are arriving, but none from another device has asked for a media server "
                     "yet. Open the DLNA list in DeoVR or Skybox now.")
        else:
            text += ("<br>No searches have arrived yet. Open the DLNA list in DeoVR or Skybox now - if this "
                     "stays empty, something between the headset and this PC is blocking discovery, most "
                     "often a firewall.")
        if s["failed"]:
            text += (f"<br><b>{s['failed']} replies could not be sent</b> - Windows said: {s['last_failure']}. "
                     "Security software on this PC is the usual cause.")
        self.status.setText(text)

    def _refresh_firewall(self) -> None:
        if not firewall.supported() or not self.server.running:
            self.firewall_label.setText("")
            self.firewall_btn.hide()
            return
        allowed = firewall.dlna_rules_exist(self.server.port)
        self.firewall_btn.setVisible(not allowed)
        self.firewall_btn.setEnabled(not allowed)
        self.firewall_label.setText(
            "Windows Firewall lets players on your network in." if allowed else
            "<b>Windows Firewall is blocking DLNA.</b> Players will not see this library until it is allowed.")

    def _allow(self) -> None:
        if not firewall.request_dlna_rules(self.server.port):
            QMessageBox.information(self, "Not allowed", "Windows did not add the rules. They need the "
                                    "permission prompt to be accepted.")
            return
        self.firewall_btn.setEnabled(False)
        self.firewall_label.setText("Waiting for Windows to add the rules…")
        self._waiting = 0
        QTimer.singleShot(700, self._poll)

    def _poll(self) -> None:
        self._waiting += 1
        if firewall.dlna_rules_exist(self.server.port) or self._waiting > 20:
            self._refresh_firewall()
            return
        QTimer.singleShot(700, self._poll)
