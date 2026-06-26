import sys
import time
import os
import logging
import zlib
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMessageBox, QWidget, QLabel, QPushButton, QComboBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog,
    QLineEdit, QTextEdit, QProgressBar, QCheckBox, QGroupBox
)
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont


APP_NAME = "Transcell PH | STM32 CAN Flasher v1.1 Factory"
BOOTLOADER_BITRATE = 125000
DEFAULT_APP_TEST_SECONDS = 10


def get_app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def crc32_file(path: str) -> str:
    crc = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            crc = zlib.crc32(chunk, crc)
    return f"0x{crc & 0xFFFFFFFF:08X}"


class WorkerBase(QThread):
    line_received = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self.cancel_flag = False

    def cancel(self):
        self.cancel_flag = True


class IntegratedCanprogWorker(WorkerBase):
    """Integrated CANPROG engine worker.

    Bootloader mode is fixed to IXXAT channel 0 @ 125 kbit/s.
    Uses canprog modules directly, not canprog.exe.
    """

    def __init__(self, operation, hex_file=None, address=0x08000000,
                 erase=True, verify=True, go=True, read_size=0x10):
        super().__init__()
        self.operation = operation
        self.hex_file = hex_file
        self.address = address
        self.erase_enabled = erase
        self.verify_enabled = verify
        self.go_enabled = go
        self.read_size = read_size
        self.current_phase = ""
        self.start_time = None

    def _handle_log_for_progress(self, text: str):
        if "Erasing memory" in text or "Mass erasing" in text:
            self.current_phase = "erase"
            self.progress_signal.emit(5)
        elif "Writing memory" in text:
            self.current_phase = "write"
            self.progress_signal.emit(15)
        elif "Verifying memory" in text:
            self.current_phase = "verify"
            self.progress_signal.emit(75)
        elif "Starting application" in text:
            self.current_phase = "go"
            self.progress_signal.emit(95)

        if "Progress:" in text and "%" in text:
            try:
                percent_text = text.split("Progress:")[1].split("%")[0].strip()
                value = int(percent_text)

                if self.current_phase == "write":
                    mapped = 15 + int(value * 0.60)
                elif self.current_phase == "verify":
                    mapped = 75 + int(value * 0.20)
                elif self.current_phase == "erase":
                    mapped = int(value * 0.15)
                else:
                    mapped = value

                self.progress_signal.emit(max(0, min(100, mapped)))
            except Exception:
                pass

    def _emit_log(self, text: str):
        self.line_received.emit(text)
        self._handle_log_for_progress(text)

    def run(self):
        iface = None
        handler = None
        protocol = None
        connected = False
        self.start_time = time.time()

        try:
            import can
            from canprog import protocols
            from canprog import file as canprog_file
            from canprog.logger import log
            from canprog.main import connect, disconnect, erase, write, verify, go, read

            class QtLogHandler(logging.Handler):
                def __init__(self, callback):
                    super().__init__()
                    self.callback = callback

                def emit(self, record):
                    timestamp = time.strftime("%H:%M:%S")
                    self.callback(
                        f"[{timestamp}] {record.name} {record.levelname}: {record.getMessage()}"
                    )

            handler = QtLogHandler(self._emit_log)
            log.addHandler(handler)
            log.setLevel(logging.INFO)

            self._emit_log("Opening IXXAT bootloader CAN bus @125 kbit/s...")
            iface = can.interface.Bus(interface="ixxat", channel=0, bitrate=BOOTLOADER_BITRATE)

            protocol_class = protocols.get_protocol_class_by_name("stm32")
            protocol = protocol_class(iface)

            connect(protocol)
            connected = True

            if self.cancel_flag:
                raise InterruptedError("Cancelled by user")

            if self.operation == "read_info":
                read(protocol, self.address, self.read_size)

            elif self.operation == "write":
                if not self.hex_file or not os.path.exists(self.hex_file):
                    raise FileNotFoundError("HEX file does not exist.")

                datafile = canprog_file.FileManager()

                if self.erase_enabled:
                    erase(protocol, [])
                    self.progress_signal.emit(15)

                if self.cancel_flag:
                    raise InterruptedError("Cancelled by user")

                datafile.load(self.hex_file, "hex", self.address)
                for segment_address, data in datafile.get_segments():
                    if self.cancel_flag:
                        raise InterruptedError("Cancelled by user")
                    write(protocol, segment_address, data)

                if self.verify_enabled:
                    for segment_address, data in datafile.get_segments():
                        if self.cancel_flag:
                            raise InterruptedError("Cancelled by user")
                        verify(protocol, segment_address, data)

                if self.go_enabled:
                    go(protocol, self.address)

            else:
                raise ValueError(f"Unknown CANPROG operation: {self.operation}")

            disconnect(protocol)
            connected = False

            elapsed = time.time() - self.start_time
            self.progress_signal.emit(100)
            self.finished_signal.emit(0, f"Completed in {elapsed:.1f}s")

        except InterruptedError as e:
            self.finished_signal.emit(-2, str(e))

        except Exception as e:
            msg = str(e)
            lower = msg.lower()
            if (
                "error warning limit exceeded" in lower
                or "connecting error" in lower
                or "vcierror" in lower
            ):
                msg = (
                    "No communication with STM32 Bootloader. Check BOOT0/BOOT1, "
                    "125 kbit/s bitrate, CANH/CANL wiring, termination, board power, "
                    "and bootloader CAN pin routing."
                )
            self.line_received.emit(f"ERROR: {msg}")
            self.finished_signal.emit(-1, msg)

        finally:
            try:
                if connected and protocol is not None:
                    from canprog.main import disconnect
                    disconnect(protocol)
            except Exception:
                pass

            try:
                if iface is not None:
                    iface.shutdown()
            except Exception:
                pass

            try:
                if handler is not None:
                    from canprog.logger import log
                    log.removeHandler(handler)
            except Exception:
                pass


class AppTestWorker(WorkerBase):
    """Application-mode CAN receive test."""

    def __init__(self, bitrate=250000, duration=DEFAULT_APP_TEST_SECONDS):
        super().__init__()
        self.bitrate = bitrate
        self.duration = duration

    def run(self):
        bus = None
        try:
            import can
            bus = can.Bus(interface="ixxat", channel=0, bitrate=self.bitrate)
            self.line_received.emit(f"Application CAN monitor started @{self.bitrate} bit/s")

            count = 0
            start = time.time()

            while (time.time() - start) < self.duration and not self.cancel_flag:
                msg = bus.recv(0.5)
                if msg is not None:
                    count += 1
                    self.line_received.emit(
                        f"RX ID=0x{msg.arbitration_id:X}, "
                        f"EXT={msg.is_extended_id}, "
                        f"DLC={msg.dlc}, "
                        f"DATA={msg.data.hex(' ').upper()}"
                    )

            if self.cancel_flag:
                self.finished_signal.emit(-2, "Application test cancelled")
            elif count > 0:
                self.finished_signal.emit(0, f"Application communication PASS. Frames received: {count}")
            else:
                self.line_received.emit(
                    "ERROR: No application CAN frames received. Check application mode, "
                    "selected bitrate, CANH/CANL wiring, termination, common GND, "
                    "board power, or application CAN configuration."
                )
                self.finished_signal.emit(-1, "Application communication FAIL. Frames received: 0")

        except Exception as e:
            self.line_received.emit(f"ERROR: {str(e)}")
            self.finished_signal.emit(-1, f"Error: {str(e)}")
        finally:
            try:
                if bus is not None:
                    bus.shutdown()
            except Exception:
                pass


class STM32FactoryProgrammer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1240, 720)
        self.current_worker = None
        self.log_file = None
        self.connected = False
        self.tx_count = 0
        self.rx_count = 0
        self.ok_count = 0
        self.fail_count = 0
        self.total_count = 0
        self.setup_logging()
        self.init_ui()

    def setup_logging(self):
        log_dir = os.path.join(get_app_dir(), "factory_logs")
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"flash_{timestamp}.log")
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"=== Session started {timestamp} ===\n")

    def log(self, message):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')}: {message}\n")
        except Exception:
            pass

        self.console.append(message)
        scrollbar = self.console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

        if "RX ID=" in message or "RX " in message:
            self.rx_count += 1
        if "TX " in message:
            self.tx_count += 1
        self.update_stats()

        if "Bootloader version:" in message:
            version = message.split("Bootloader version:")[1].strip()
            self.lbl_boot_ver.setText(version)
            self.lbl_boot_ver.setStyleSheet("color: #3fb950; font-weight: bold;")
        elif "Chip ID:" in message:
            chip = message.split("Chip ID:")[1].strip()
            self.lbl_chip_id.setText(chip)
            self.lbl_chip_id.setStyleSheet("color: #3fb950; font-weight: bold;")

        if "Mass erasing" in message or "Erasing memory" in message:
            self.set_status("Erasing Flash...", "#f2cc60")
        elif "Writing memory" in message:
            self.set_status("Writing Firmware...", "#58a6ff")
        elif "Verifying memory" in message:
            self.set_status("Verifying Firmware...", "#bc8cff")
        elif "Starting application" in message:
            self.set_status("Starting Application...", "#3fb950")

    def set_status(self, text, color="#888"):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"color: {color}; font-weight: bold;")
        self.lbl_footer.setText(f"Factory Mode | {text}")

    def update_stats(self):
        self.lbl_stats.setText(
            f"TOTAL: {self.total_count} | OK: {self.ok_count} | FAIL: {self.fail_count} | RX: {self.rx_count}"
        )

    def init_ui(self):
        outer = QHBoxLayout(self)
        outer.setSpacing(15)

        left = QVBoxLayout()
        left.setSpacing(10)

        header = QGroupBox()
        header.setStyleSheet("border:0;")
        hbox = QHBoxLayout(header)
        title = QLabel("STM32 CAN Bootloader")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        self.lbl_online = QLabel("● OFFLINE")
        self.lbl_online.setStyleSheet("color: #ff4d4d; font-weight: bold; font-size: 12px;")
        hbox.addWidget(title)
        hbox.addStretch()
        hbox.addWidget(self.lbl_online)
        left.addWidget(header)

        conn_group = QGroupBox("CAN Interface")
        grid = QGridLayout(conn_group)
        grid.addWidget(QLabel("Interface:"), 0, 0)
        self.cmb_interface = QComboBox()
        self.cmb_interface.addItems(["IXXAT USB-CAN V2"])
        grid.addWidget(self.cmb_interface, 0, 1)
        grid.addWidget(QLabel("Bitrate:"), 1, 0)
        self.cmb_bitrate = QComboBox()
        self.cmb_bitrate.addItems(["125 kbit/s", "250 kbit/s", "500 kbit/s", "1000 kbit/s"])
        grid.addWidget(self.cmb_bitrate, 1, 1)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.connect_can)
        grid.addWidget(self.btn_connect, 0, 2, 2, 1)
        left.addWidget(conn_group)

        info_group = QGroupBox("Bootloader Info")
        info_grid = QGridLayout(info_group)
        info_grid.addWidget(QLabel("Bootloader Version:"), 0, 0)
        self.lbl_boot_ver = QLabel("Not Read")
        self.lbl_boot_ver.setStyleSheet("color: #f2cc60; font-weight: bold;")
        info_grid.addWidget(self.lbl_boot_ver, 0, 1)
        info_grid.addWidget(QLabel("Chip ID:"), 1, 0)
        self.lbl_chip_id = QLabel("Unknown")
        self.lbl_chip_id.setStyleSheet("color: #f2cc60; font-weight: bold;")
        info_grid.addWidget(self.lbl_chip_id, 1, 1)
        info_grid.addWidget(QLabel("Bootloader bitrate:"), 2, 0)
        info_grid.addWidget(QLabel("125 kbit/s fixed"), 2, 1)
        left.addWidget(info_group)

        production_group = QGroupBox("Production Traceability")
        prod_grid = QGridLayout(production_group)
        prod_grid.addWidget(QLabel("Operator:"), 0, 0)
        self.txt_operator = QLineEdit()
        self.txt_operator.setPlaceholderText("Operator name / ID")
        prod_grid.addWidget(self.txt_operator, 0, 1)
        prod_grid.addWidget(QLabel("Unit S/N:"), 1, 0)
        self.txt_unit_sn = QLineEdit()
        self.txt_unit_sn.setPlaceholderText("PCB / Product serial number")
        prod_grid.addWidget(self.txt_unit_sn, 1, 1)
        left.addWidget(production_group)

        fw_group = QGroupBox("Firmware")
        fw_layout = QGridLayout(fw_group)
        self.txt_firmware = QLineEdit()
        self.txt_firmware.setPlaceholderText("Path to .hex file")
        self.btn_browse = QPushButton("...")
        self.btn_browse.clicked.connect(self.browse_firmware)
        fw_layout.addWidget(QLabel("File:"), 0, 0)
        fw_layout.addWidget(self.txt_firmware, 0, 1)
        fw_layout.addWidget(self.btn_browse, 0, 2)
        self.lbl_file_info = QLabel("No file selected")
        self.lbl_file_info.setStyleSheet("color: #aaa; font-size: 11px;")
        fw_layout.addWidget(self.lbl_file_info, 1, 1, 1, 2)
        left.addWidget(fw_group)

        action_group = QGroupBox("Actions")
        vbox = QVBoxLayout(action_group)
        self.btn_read_info = QPushButton("📄 Read Device Info")
        self.btn_read_info.clicked.connect(self.read_device_info)
        vbox.addWidget(self.btn_read_info)

        self.btn_test_app = QPushButton("🧪 Test Application CAN")
        self.btn_test_app.clicked.connect(self.test_application_can)
        vbox.addWidget(self.btn_test_app)

        self.btn_full_flash = QPushButton("⚡ Flash Firmware")
        self.btn_full_flash.clicked.connect(self.flash_firmware)
        self.btn_full_flash.setStyleSheet("background-color: #1a5fb4; color: white; font-weight: bold;")
        vbox.addWidget(self.btn_full_flash)

        self.chk_skip_erase = QCheckBox("Skip erase (only if already blank)")
        vbox.addWidget(self.chk_skip_erase)
        left.addWidget(action_group)

        prog_group = QGroupBox("Progress")
        prog_layout = QVBoxLayout(prog_group)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        prog_layout.addWidget(self.progress_bar)
        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet("font-weight: bold; color: #888;")
        prog_layout.addWidget(self.lbl_status)
        left.addWidget(prog_group)

        left.addStretch()
        outer.addLayout(left, 40)

        right = QVBoxLayout()
        console_group = QGroupBox("Factory Log")
        console_layout = QVBoxLayout(console_group)
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(QFont("Consolas", 10))
        self.console.setPlainText(
            "STM32 CAN Bootloader Programmer\n"
            "Integrated CANPROG Engine\n"
            "Factory Mode Ready. Click Connect to detect IXXAT interface."
        )
        console_layout.addWidget(self.console)

        toolbar = QHBoxLayout()
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.console.clear)
        self.btn_save_log = QPushButton("Save Log")
        self.btn_save_log.clicked.connect(self.save_log)
        toolbar.addWidget(self.btn_clear)
        toolbar.addWidget(self.btn_save_log)
        toolbar.addStretch()
        console_layout.addLayout(toolbar)
        right.addWidget(console_group, 1)

        footer = QHBoxLayout()
        self.lbl_footer = QLabel("Factory Mode | Ready")
        footer.addWidget(self.lbl_footer)
        footer.addStretch()
        self.lbl_stats = QLabel("TOTAL: 0 | OK: 0 | FAIL: 0 | RX: 0")
        footer.addWidget(self.lbl_stats)
        right.addLayout(footer)
        outer.addLayout(right, 60)

        self.apply_styles()
        self.update_ui_state(False)
        self.btn_browse.setEnabled(False)
        self.btn_read_info.setEnabled(False)
        self.btn_test_app.setEnabled(False)
        self.btn_full_flash.setEnabled(False)

    def apply_styles(self):
        self.setStyleSheet("""
            QWidget { background-color: #0d1117; color: #c9d1d9; }
            QGroupBox {
                border: 1px solid #30363d;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 10px;
                font-weight: bold;
            }
            QPushButton {
                background-color: #21262d;
                border: 1px solid #30363d;
                border-radius: 4px;
                padding: 8px 12px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #30363d; }
            QPushButton:disabled { color: #666; background-color: #161b22; }
            QLineEdit, QComboBox {
                background-color: #0d1117;
                border: 1px solid #30363d;
                border-radius: 4px;
                padding: 6px;
            }
            QTextEdit {
                background-color: #0d1117;
                border: 1px solid #30363d;
                font-family: Consolas;
                color: #c9d1d9;
            }
            QProgressBar {
                border: 1px solid #30363d;
                border-radius: 4px;
                text-align: center;
                background-color: #0d1117;
                height: 16px;
            }
            QProgressBar::chunk {
                background-color: #238636;
                border-radius: 4px;
            }
        """)

    def update_ui_state(self, busy):
        online = self.connected
        has_hex = self.txt_firmware.text().lower().endswith(".hex") and os.path.exists(self.txt_firmware.text())
        self.btn_connect.setEnabled(not busy)
        self.btn_browse.setEnabled(not busy and online)
        self.btn_read_info.setEnabled(not busy and online)
        self.btn_test_app.setEnabled(not busy and online)
        self.btn_full_flash.setEnabled(not busy and online and has_hex)
        self.cmb_interface.setEnabled(not busy)
        self.cmb_bitrate.setEnabled(not busy)
        self.txt_operator.setEnabled(not busy)
        self.txt_unit_sn.setEnabled(not busy)

    def get_selected_bitrate(self):
        text = self.cmb_bitrate.currentText()
        if "1000" in text:
            return 1000000
        if "500" in text:
            return 500000
        if "250" in text:
            return 250000
        return 125000

    def connect_can(self):
        self.log("Detecting IXXAT interface...")
        try:
            import can
            # Adapter detection only. Bootloader commands still use fixed 125 kbit/s.
            bus = can.Bus(interface="ixxat", channel=0, bitrate=BOOTLOADER_BITRATE)
            bus.shutdown()
            self.set_connected(True)
            self.log("✓ IXXAT detected successfully")
        except Exception as e:
            self.set_connected(False)
            self.log(f"ERROR: IXXAT detection failed: {str(e)}")

    def set_connected(self, connected):
        self.connected = connected
        if connected:
            self.lbl_online.setText("● ONLINE")
            self.lbl_online.setStyleSheet("color: #3fb950; font-weight: bold; font-size: 12px;")
            self.set_status("Connected", "#3fb950")
            self.btn_connect.setText("Disconnect")
            try:
                self.btn_connect.clicked.disconnect()
            except Exception:
                pass
            self.btn_connect.clicked.connect(self.disconnect_can)
        else:
            self.lbl_online.setText("● OFFLINE")
            self.lbl_online.setStyleSheet("color: #ff4d4d; font-weight: bold; font-size: 12px;")
            self.set_status("Offline", "#888")
            self.btn_connect.setText("Connect")
            try:
                self.btn_connect.clicked.disconnect()
            except Exception:
                pass
            self.btn_connect.clicked.connect(self.connect_can)
        self.update_ui_state(False)

    def disconnect_can(self):
        self.log("CAN interface disconnected")
        self.lbl_boot_ver.setText("Not Read")
        self.lbl_boot_ver.setStyleSheet("color: #f2cc60; font-weight: bold;")
        self.lbl_chip_id.setText("Unknown")
        self.lbl_chip_id.setStyleSheet("color: #f2cc60; font-weight: bold;")
        self.progress_bar.setValue(0)
        self.set_connected(False)

    def browse_firmware(self):
        file, _ = QFileDialog.getOpenFileName(self, "Select HEX File", "", "HEX Files (*.hex);;All Files (*.*)")
        if not file:
            return
        self.txt_firmware.setText(file)
        size = os.path.getsize(file)
        crc = crc32_file(file)
        self.lbl_file_info.setText(f"{size:,} bytes ({size/1024:.1f} KB) | Intel HEX | CRC32 {crc}")
        self.log(f"Selected firmware: {file}")
        self.log(f"Firmware size: {size:,} bytes | CRC32: {crc}")
        self.update_ui_state(False)

    def _require_bootloader_bitrate(self, action_name):
        if self.get_selected_bitrate() != BOOTLOADER_BITRATE:
            QMessageBox.warning(
                self,
                "Wrong Bitrate",
                f"{action_name} requires STM32 ROM bootloader mode at 125 kbit/s.\n\n"
                "Please select 125 kbit/s."
            )
            return False
        return True

    def read_device_info(self):
        if not self._require_bootloader_bitrate("Device info"):
            return
        self.log("Reading bootloader information...")
        self.progress_bar.setValue(0)
        self.current_worker = IntegratedCanprogWorker(operation="read_info", read_size=0x10)
        self.connect_worker(self.current_worker)
        self.current_worker.start()

    def test_application_can(self):
        bitrate = self.get_selected_bitrate()
        if bitrate == BOOTLOADER_BITRATE:
            QMessageBox.warning(
                self,
                "Wrong Bitrate",
                "Application test is intended for application mode.\n\n"
                "Please select 250, 500, or 1000 kbit/s."
            )
            return
        self.log(f"Starting application CAN test at {bitrate} bit/s...")
        self.progress_bar.setValue(0)
        self.current_worker = AppTestWorker(bitrate=bitrate, duration=DEFAULT_APP_TEST_SECONDS)
        self.connect_worker(self.current_worker)
        self.current_worker.start()

    def flash_firmware(self):
        file = self.txt_firmware.text().strip()
        if not file.lower().endswith(".hex") or not os.path.exists(file):
            QMessageBox.critical(self, "Invalid File", "Select a valid .hex file.")
            return
        if not self._require_bootloader_bitrate("Flashing"):
            return

        operator = self.txt_operator.text().strip() or "N/A"
        unit_sn = self.txt_unit_sn.text().strip() or "N/A"
        crc = crc32_file(file)

        reply = QMessageBox.question(
            self,
            "Confirm Flash",
            f"Firmware: {os.path.basename(file)}\n"
            f"CRC32: {crc}\n"
            f"Operator: {operator}\n"
            f"Unit S/N: {unit_sn}\n\n"
            "Proceed with erase/write/verify/go?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.log("Flash cancelled by user")
            return

        self.log("=== FLASH START ===")
        self.log(f"Operator: {operator}")
        self.log(f"Unit S/N: {unit_sn}")
        self.log(f"Firmware: {file}")
        self.log(f"Firmware CRC32: {crc}")
        self.progress_bar.setValue(0)

        self.current_worker = IntegratedCanprogWorker(
            operation="write",
            hex_file=file,
            address=0x08000000,
            erase=not self.chk_skip_erase.isChecked(),
            verify=True,
            go=True,
            read_size=0x10
        )
        self.connect_worker(self.current_worker)
        self.current_worker.start()

    def connect_worker(self, worker):
        self.update_ui_state(True)
        worker.line_received.connect(self.clean_and_log)
        worker.progress_signal.connect(self.progress_bar.setValue)
        worker.finished_signal.connect(self.operation_finished)

    def clean_and_log(self, text):
        cleaned = self.clean_canprog_error(text)
        if cleaned is None:
            return
        self.log(cleaned)

    def clean_canprog_error(self, text):
        lower = text.lower().strip()
        noisy = [
            "can acknowledgment error",
            "can bit error",
            "can bit stuff error",
            "can form error",
            "can message flags",
            "unexpected message info type",
            "traceback",
            "file ",
            "line ",
            "sys.exit",
            "connect(protocol)",
            "protocol.connect",
            "self._connect",
            "self._init",
            "self._recv",
            "frame =",
            "msg, already_filtered",
            "return self.bus",
        ]
        if any(item in lower for item in noisy) or "~~~~" in text:
            return None
        return text

    def operation_finished(self, code, summary):
        self.update_ui_state(False)
        self.current_worker = None
        self.total_count += 1

        if code == 0:
            self.ok_count += 1
            self.progress_bar.setValue(100)
            self.set_status("Success", "#3fb950")
            self.log(f"✓ Operation successful: {summary}")
        elif code == -2:
            self.set_status("Cancelled", "#f2cc60")
            self.log(f"Operation cancelled: {summary}")
        else:
            self.fail_count += 1
            self.set_status("Failed", "#f85149")
            self.log(f"✗ Operation failed: {summary}")
        self.update_stats()

    def save_log(self):
        file, _ = QFileDialog.getSaveFileName(self, "Save Log", "", "Text Files (*.txt)")
        if file:
            with open(file, "w", encoding="utf-8") as f:
                f.write(self.console.toPlainText())
            self.log(f"Log saved to {file}")

    def closeEvent(self, event):
        if self.current_worker and self.current_worker.isRunning():
            reply = QMessageBox.question(
                self,
                "Active Operation",
                "A task is running. Really close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            self.current_worker.cancel()
            self.current_worker.wait(2000)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = STM32FactoryProgrammer()
    window.show()
    sys.exit(app.exec())
