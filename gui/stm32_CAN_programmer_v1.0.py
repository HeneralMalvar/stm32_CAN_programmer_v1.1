import sys
import time
import os
import logging
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMessageBox, QWidget, QLabel, QPushButton, QComboBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog,
    QLineEdit, QTextEdit, QProgressBar, QCheckBox, QGroupBox
)
from PyQt6.QtCore import QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont


class WorkerBase(QThread):
    """Base worker for CAN operations."""
    line_received = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal(int, str)  # code, summary

    def __init__(self):
        super().__init__()
        self.cancel_flag = False

    def cancel(self):
        self.cancel_flag = True


class IntegratedCanprogWorker(WorkerBase):
    """Integrated CANPROG engine worker.

    This uses the canprog modules directly instead of launching canprog.exe.
    Bootloader operations are fixed to IXXAT channel 0 @ 125 kbit/s.
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
                    try:
                        timestamp = time.strftime("%H:%M:%S")
                        self.callback(
                            f"[{timestamp}] {record.name} {record.levelname}: {record.getMessage()}"
                        )
                    except Exception:
                        pass

            handler = QtLogHandler(self._emit_log)
            log.addHandler(handler)
            log.setLevel(logging.INFO)

            self._emit_log("Opening IXXAT bootloader CAN bus @125 kbit/s...")
            iface = can.interface.Bus(interface='ixxat', channel=0, bitrate=125000)

            protocol_class = protocols.get_protocol_class_by_name('stm32')
            protocol = protocol_class(iface)

            time.sleep(0.3)
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
                    BOOTLOADER_BITRATE = 125000
                    erase(protocol, [])
                    self.progress_signal.emit(15)

                    self._emit_log("Erase completed successfully.")
                    self._emit_log("Waiting 2 seconds before write...")
                    time.sleep(2.0)

                    self._emit_log("Releasing IXXAT CAN interface after erase...")

                    try:
                        disconnect(protocol)
                    except Exception:
                        pass

                    connected = False

                    try:
                        iface.shutdown()
                    except Exception:
                        pass

                    time.sleep(1.0)

                    self._emit_log("Re-opening IXXAT bootloader CAN bus @125 kbit/s...")
                    iface = can.interface.Bus(
                        interface="ixxat",
                        channel=0,
                        bitrate=BOOTLOADER_BITRATE
                    )

                    time.sleep(0.5)

                    protocol = protocol_class(iface)

                    self._emit_log("Reconnecting to STM32 bootloader after erase...")
                    connect(protocol)
                    connected = True

                    time.sleep(0.5)

                    self._emit_log("Reconnect successful. Starting write operation...")

                if self.cancel_flag:
                    raise InterruptedError("Cancelled by user")

                datafile.load(self.hex_file, 'hex', self.address)

                for segment_address, data in datafile.get_segments():
                    if self.cancel_flag:
                        raise InterruptedError("Cancelled by user")

                    self._emit_log(
                        f"Writing segment at 0x{segment_address:08X}, size {len(data)} bytes..."
                    )

                    write(protocol, segment_address, data)
                    time.sleep(0.2)

                if self.verify_enabled:
                    self._emit_log("Writing completed. Waiting before verification...")
                    time.sleep(0.5)

                    for segment_address, data in datafile.get_segments():
                        if self.cancel_flag:
                            raise InterruptedError("Cancelled by user")

                        self._emit_log(
                            f"Verifying segment at 0x{segment_address:08X}, size {len(data)} bytes..."
                        )

                        verify(protocol, segment_address, data)
                        time.sleep(0.2)

                if self.go_enabled:
                    self._emit_log("Starting application...")
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
            raw_msg = str(e)
            self.line_received.emit(f"RAW ERROR: {raw_msg}")

            msg = raw_msg
            lower = msg.lower()
            if "error warning limit exceeded" in lower or "connecting error" in lower:
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
    """Application-mode CAN receive test. Default is 250 kbit/s."""

    def __init__(self, bitrate=250000, duration=10):
        super().__init__()
        self.bitrate = bitrate
        self.duration = duration

    def run(self):
        try:
            import can
            bus = can.Bus(interface='ixxat', channel=0, bitrate=self.bitrate)
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

            bus.shutdown()

            if self.cancel_flag:
                self.finished_signal.emit(-2, "Cancelled")
            elif count > 0:
                self.finished_signal.emit(0, f"Frames received: {count} - PASS")
            else:
                self.line_received.emit(
                    "ERROR: No application CAN frames received. Check application mode, "
                    "250 kbit/s bitrate, CANH/CANL wiring, termination, common GND, "
                    "board power, or application CAN configuration."
                )
                self.finished_signal.emit(-1, "Frames received: 0 - FAIL")

        except Exception as e:
            self.line_received.emit(f"ERROR: {str(e)}")
            self.finished_signal.emit(-1, f"Error: {str(e)}")


class STM32FactoryProgrammer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Transcell PH | STM32 CAN Flasher v1.1 (Trial)")
        self.resize(1200, 700)
        self.current_worker = None
        self.log_file = None
        self.tx_count = 0
        self.rx_count = 0
        self.ok_count = 0
        self.fail_count = 0
        self.setup_logging()
        self.init_ui()
        self.workers = []
        

    def setup_logging(self):
        log_dir = "factory_logs"
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"flash_{timestamp}.log")
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(f"=== Session started {timestamp} ===\n")

    def log(self, message):
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(f"{datetime.now().isoformat()}: {message}\n")
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

        # Bootloader info parsing
        if "Bootloader version:" in message:
            version = message.split("Bootloader version:")[1].strip()
            self.lbl_boot_ver.setText(version)
            self.lbl_boot_ver.setStyleSheet("color: #3fb950; font-weight: bold;")
        elif "Chip ID:" in message:
            chip = message.split("Chip ID:")[1].strip()
            self.lbl_chip_id.setText(chip)
            self.lbl_chip_id.setStyleSheet("color: #3fb950; font-weight: bold;")

        # Phase/status detection
        if "Mass erasing" in message:
            self.lbl_status.setText("Erasing Flash...")
            self.lbl_status.setStyleSheet("color: #f2cc60; font-weight: bold;")
        elif "Writing memory" in message:
            self.lbl_status.setText("Writing Firmware...")
            self.lbl_status.setStyleSheet("color: #58a6ff; font-weight: bold;")
        elif "Verifying memory" in message:
            self.lbl_status.setText("Verifying Firmware...")
            self.lbl_status.setStyleSheet("color: #bc8cff; font-weight: bold;")
        elif "Starting application" in message:
            self.lbl_status.setText("Starting Application...")
            self.lbl_status.setStyleSheet("color: #3fb950; font-weight: bold;")

    def update_stats(self):
        self.lbl_stats.setText(
            f"TX: {self.tx_count} | RX: {self.rx_count} | OK: {self.ok_count} | FAIL: {self.fail_count}"
        )

    def init_ui(self):
        outer = QHBoxLayout(self)
        outer.setSpacing(15)

        left = QVBoxLayout()
        left.setSpacing(10)

        # Header
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

        # Connection
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

        # Bootloader info
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
        info_grid.addWidget(QLabel("125 kbit/s"), 2, 1)
        left.addWidget(info_group)

        # Firmware
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

        # Actions
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

        # Progress
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

        # Right panel - Console
        right = QVBoxLayout()
        console_group = QGroupBox("Log")
        console_layout = QVBoxLayout(console_group)
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(QFont("Consolas", 10))
        self.console.setPlainText(
            "STM32 CAN Bootloader Programmer\n"
            "Integrated CANPROG Engine\n"
            "System Ready. Click Connect to detect IXXAT interface."
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

        # Footer
        footer = QHBoxLayout()
        self.lbl_footer = QLabel("Factory Mode | Ready")
        footer.addWidget(self.lbl_footer)
        footer.addStretch()
        self.lbl_stats = QLabel("TX: 0 | RX: 0 | OK: 0 | FAIL: 0")
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
        self.btn_connect.setEnabled(not busy)
        self.btn_browse.setEnabled(not busy and self.lbl_online.text() != "● OFFLINE")
        self.btn_read_info.setEnabled(not busy and self.lbl_online.text() != "● OFFLINE")
        self.btn_test_app.setEnabled(not busy and self.lbl_online.text() != "● OFFLINE")
        self.btn_full_flash.setEnabled(not busy and self.lbl_online.text() != "● OFFLINE" and self.txt_firmware.text().lower().endswith(".hex"))
        self.cmb_interface.setEnabled(not busy)
        self.cmb_bitrate.setEnabled(not busy)
    def get_selected_bitrate(self):
        text = self.cmb_bitrate.currentText()

        if "1000" in text:
            return 1000000
        elif "500" in text:
            return 500000
        elif "250" in text:
            return 250000
        else:
            return 125000
        
    def connect_can(self):
        self.log("Detecting IXXAT interface...")
        try:
            import can
            # Open/close at 125k only to confirm that the adapter exists.
            bus = can.Bus(interface='ixxat', channel=0, bitrate=125000)
            bus.shutdown()
            self.set_connected(True)
            self.log("✓ IXXAT detected successfully")
        except Exception as e:
            self.set_connected(False)
            self.log(f"ERROR: IXXAT detection failed: {str(e)}")

    def set_connected(self, connected):
        if connected:
            self.lbl_online.setText("● ONLINE")
            self.lbl_online.setStyleSheet("color: #3fb950; font-weight: bold; font-size: 12px;")
            self.lbl_status.setText("Connected")
            self.lbl_status.setStyleSheet("color: #3fb950; font-weight: bold;")
            self.btn_connect.setText("Disconnect")
            try:
                self.btn_connect.clicked.disconnect()
            except Exception:
                pass
            self.btn_connect.clicked.connect(self.disconnect_can)
        else:
            self.lbl_online.setText("● OFFLINE")
            self.lbl_online.setStyleSheet("color: #ff4d4d; font-weight: bold; font-size: 12px;")
            self.lbl_status.setText("Offline")
            self.lbl_status.setStyleSheet("color: #888;")
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
        if file:
            self.txt_firmware.setText(file)
            size = os.path.getsize(file)
            self.lbl_file_info.setText(f"{size:,} bytes ({size/1024:.1f} KB) | Intel HEX")
            self.log(f"Selected firmware: {file}")
            self.update_ui_state(False)

    def is_busy(self):
        if self.current_worker is not None and self.current_worker.isRunning():
            QMessageBox.warning(self, "Busy", "Operation is already in progress.")
            return True
        return False

    def read_device_info(self):
        if self.is_busy():
            return
        if self.get_selected_bitrate() != 125000:
            QMessageBox.warning(self, "Wrong Bitrate", "Device info requires 125 kbit/s bootloader mode.")
            return
        self.log("Reading bootloader information...")
        self.progress_bar.setValue(0)
        self.current_worker = IntegratedCanprogWorker(operation="read_info", read_size=0x10)
        self.connect_worker(self.current_worker)
        self.current_worker.start()

    def test_application_can(self):
        if self.is_busy():
            return
        if "125" in self.cmb_bitrate.currentText():
            QMessageBox.warning(self, "Wrong Bitrate", "Application test requires application bitrate.\n\nPlease select 250, 500, or 1000 kbit/s.")
            return
        self.log("Starting application CAN test...")
        self.progress_bar.setValue(0)
        self.current_worker = AppTestWorker(bitrate=self.get_selected_bitrate(), duration=10)
        self.connect_worker(self.current_worker)
        self.current_worker.start()

    def flash_firmware(self):
        if self.is_busy():
            return
        file = self.txt_firmware.text().strip()
        if not file.lower().endswith(".hex") or not os.path.exists(file):
            QMessageBox.critical(self, "Invalid File", "Select a valid .hex file.")
            return
        if self.get_selected_bitrate()!= 125000:
            QMessageBox.warning(self, "Wrong Bitrate", "Flashing requires 125 kbit/s bootloader mode.")
            return

        reply = QMessageBox.question(
            self,
            "Confirm Flash",
            f"Flash {os.path.basename(file)} to device?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.log("Flash cancelled by user")
            return

        self.log(f"Starting integrated flash: {file}")
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

        self.current_worker = worker
        self.workers.append(worker)

        worker.line_received.connect(self.clean_and_log)
        worker.progress_signal.connect(self.progress_bar.setValue)
        worker.finished_signal.connect(self.operation_finished)

        worker.finished.connect(
            lambda: self.cleanup_worker(worker)
        )

    def cleanup_worker(self, worker):

        if worker in self.workers:
            self.workers.remove(worker)

        worker.deleteLater()

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

        if code == 0:
            self.ok_count += 1
            self.progress_bar.setValue(100)
            self.lbl_status.setText("Success")
            self.lbl_status.setStyleSheet("color: #3fb950; font-weight: bold;")
            self.log(f"✓ Operation successful: {summary}")
        elif code == -2:
            self.lbl_status.setText("Cancelled")
            self.lbl_status.setStyleSheet("color: #f2cc60; font-weight: bold;")
            self.log(f"Operation cancelled: {summary}")
        else:
            self.fail_count += 1
            self.lbl_status.setText("Failed")
            self.lbl_status.setStyleSheet("color: #f85149; font-weight: bold;")
            self.log(f"✗ Operation failed: {summary}")
        self.update_stats()

    def save_log(self):
        file, _ = QFileDialog.getSaveFileName(self, "Save Log", "", "Text Files (*.txt)")
        if file:
            with open(file, 'w', encoding='utf-8') as f:
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
