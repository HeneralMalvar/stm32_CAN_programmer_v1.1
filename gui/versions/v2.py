import sys
import time
import os
import json
from datetime import datetime
from PyQt6.QtWidgets import (
    QApplication, QMessageBox, QWidget, QLabel, QPushButton, QComboBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog,
    QLineEdit, QTextEdit, QProgressBar, QCheckBox, QGroupBox, QStackedWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont

class WorkerBase(QThread):
    """Base worker for CAN operations"""
    line_received = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal(int, str)  # code, summary

    def __init__(self):
        super().__init__()
        self.cancel_flag = False

    def cancel(self):
        self.cancel_flag = True


class CanprogWorker(WorkerBase):
    def __init__(self, cmd):
        super().__init__()
        self.cmd = cmd
        self.current_phase = ""
        self.start_time = None

    def run(self):
        self.start_time = time.time()
        import subprocess
        try:
            process = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )

            for line in process.stdout:
                if self.cancel_flag:
                    process.terminate()
                    self.finished_signal.emit(-2, "Cancelled by user")
                    return

                line = line.rstrip()
                self.line_received.emit(line)

                # Phase detection for progress mapping
                if "Mass erasing" in line:
                    self.current_phase = "erase"
                elif "Writing memory" in line:
                    self.current_phase = "write"
                elif "Verifying memory" in line:
                    self.current_phase = "verify"
                elif "Starting application" in line:
                    self.current_phase = "go"

                # Progress parsing
                if "Progress:" in line and "%" in line:
                    try:
                        percent_text = line.split("Progress:")[1].split("%")[0].strip()
                        value = int(percent_text)

                        mapped = 0
                        if self.current_phase == "erase":
                            mapped = int(value * 0.15)
                        elif self.current_phase == "write":
                            mapped = 15 + int(value * 0.60)
                        elif self.current_phase == "verify":
                            mapped = 75 + int(value * 0.20)
                        elif self.current_phase == "go":
                            mapped = 95 + int(value * 0.05)

                        self.progress_signal.emit(mapped if mapped <= 100 else 100)
                    except Exception:
                        pass

            process.wait()
            elapsed = time.time() - self.start_time
            summary = f"Completed in {elapsed:.1f}s"
            self.finished_signal.emit(process.returncode, summary)

        except Exception as e:
            self.finished_signal.emit(-1, f"Error: {str(e)}")


class AppTestWorker(WorkerBase):
    def __init__(self, bitrate=250000, duration=10):
        super().__init__()
        self.bitrate = bitrate
        self.duration = duration

    def run(self):
        try:
            import can
            bus = can.Bus(interface='ixxat', channel=0, bitrate=self.bitrate)
            self.line_received.emit(f"CAN bus opened at {self.bitrate} bit/s")
            count = 0
            start = time.time()

            while (time.time() - start) < self.duration and not self.cancel_flag:
                msg = bus.recv(0.5)
                if msg:
                    count += 1
                    self.line_received.emit(f"CAN ID 0x{msg.arbitration_id:X} DLC {msg.dlc}")

            bus.shutdown()
            if self.cancel_flag:
                self.finished_signal.emit(-2, "Cancelled")
            else:
                result = "PASS" if count > 0 else "FAIL"
                self.finished_signal.emit(0 if count > 0 else -1, f"Frames: {count} - {result}")
        except Exception as e:
            self.finished_signal.emit(-1, f"Error: {str(e)}")


class STM32FactoryProgrammer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Transcell PH | STM32 CAN Flasher v1.0 (Factory)")
        self.resize(1200, 700)
        self.current_worker = None
        self.log_file = None
        self.setup_logging()
        self.init_ui()

    def setup_logging(self):
        log_dir = "factory_logs"
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"flash_{timestamp}.log")
        with open(self.log_file, 'a') as f:
            f.write(f"=== Session started {timestamp} ===\n")

    def log(self, message):
        with open(self.log_file, 'a') as f:
            f.write(f"{datetime.now().isoformat()}: {message}\n")
        self.console.append(message)

    def init_ui(self):
        outer = QHBoxLayout(self)
        outer.setSpacing(15)

        # Left panel - Controls
        left = QVBoxLayout()
        left.setSpacing(10)

        # ===== Header =====
        header = QGroupBox()
        header.setStyleSheet("border:0;")
        hbox = QHBoxLayout(header)
        title = QLabel("STM32 CAN Bootloader")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        status = QLabel("● OFFLINE")
        status.setStyleSheet("color: #ff4d4d; font-weight: bold; font-size: 12px;")
        hbox.addWidget(title)
        hbox.addStretch()
        hbox.addWidget(status)
        left.addWidget(header)

        # ===== Connection =====
        conn_group = QGroupBox("CAN Interface")
        grid = QGridLayout(conn_group)
        grid.addWidget(QLabel("Interface:"), 0, 0)
        self.cmb_interface = QComboBox()
        self.cmb_interface.addItems(["IXXAT USB-CAN V2"])
        grid.addWidget(self.cmb_interface, 0, 1)
        grid.addWidget(QLabel("Bitrate:"), 1, 0)
        self.cmb_bitrate = QComboBox()
        self.cmb_bitrate.addItems(["125 kbit/s", "250 kbit/s"])
        grid.addWidget(self.cmb_bitrate, 1, 1)
        self.btn_connect = QPushButton(" Connect ")
        self.btn_connect.clicked.connect(self.connect_can)
        grid.addWidget(self.btn_connect, 0, 2, 2, 1)
        left.addWidget(conn_group)

        # ===== Firmware File =====
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

        # ===== Quick Actions =====
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

        # ===== Progress =====
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

        # ===== Right panel - Console =====
        right = QVBoxLayout()
        console_group = QGroupBox("Log")
        console_layout = QVBoxLayout(console_group)
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(QFont("Consolas", 10))
        console_layout.addWidget(self.console)

        # Console toolbar
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
        self.btn_browse.setEnabled(not busy)
        self.btn_read_info.setEnabled(not busy)
        self.btn_test_app.setEnabled(not busy)
        self.btn_full_flash.setEnabled(not busy)
        self.cmb_interface.setEnabled(not busy)
        self.cmb_bitrate.setEnabled(not busy)

    def connect_can(self):
        self.log("Detecting IXXAT interface...")
        QTimer.singleShot(500, self.simulate_connect)

    def simulate_connect(self):
        # Simulate detection - real code would use can.Bus
        self.lbl_status.setText("Connected")
        self.lbl_status.setStyleSheet("color: #3fb950; font-weight: bold;")
        self.log("✓ IXXAT detected at 125 kbit/s")
        self.update_ui_state(False)
        self.btn_connect.setText(" Disconnect ")
        self.btn_connect.clicked.disconnect()
        self.btn_connect.clicked.connect(self.disconnect_can)

    def disconnect_can(self):
        self.log("CAN interface disconnected")
        self.lbl_status.setText("Offline")
        self.lbl_status.setStyleSheet("color: #888;")
        self.btn_connect.setText(" Connect ")
        self.btn_connect.clicked.disconnect()
        self.btn_connect.clicked.connect(self.connect_can)

    def browse_firmware(self):
        file, _ = QFileDialog.getOpenFileName(self, "Select HEX File", "", "HEX Files (*.hex)")
        if file:
            self.txt_firmware.setText(file)
            size = os.path.getsize(file)
            self.lbl_file_info.setText(f"{size:,} bytes ({size/1024:.1f} KB)")
            self.log(f"Selected: {file}")

    def read_device_info(self):
        if "250" in self.cmb_bitrate.currentText():
            QMessageBox.warning(self, "Wrong Bitrate", "Device info requires 125 kbit/s.")
            return
        self.log("Reading device info...")
        self.run_canprog(["canprog", "-i", "ixxat", "stm32", "read", "info", "dummy.hex"])

    def test_application_can(self):
        if "125" in self.cmb_bitrate.currentText():
            QMessageBox.warning(self, "Wrong Bitrate", "Application test requires 250 kbit/s.")
            return
        self.log("Starting application CAN test...")
        self.current_worker = AppTestWorker(bitrate=250000, duration=10)
        self.connect_worker(self.current_worker)
        self.current_worker.start()

    def flash_firmware(self):
        file = self.txt_firmware.text().strip()
        if not file.lower().endswith(".hex"):
            QMessageBox.critical(self, "Invalid File", "Select a valid .hex file.")
            return
        if "250" in self.cmb_bitrate.currentText():
            QMessageBox.warning(self, "Wrong Bitrate", "Flashing requires 125 kbit/s.")
            return

        cmd = ["canprog", "-i", "ixxat", "stm32", "write", file, "-v", "-g"]
        if not self.chk_skip_erase.isChecked():
            cmd.append("-e")

        reply = QMessageBox.question(self, "Confirm Flash", f"Flash {os.path.basename(file)} to device?", 
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.log(f"Starting flash: {file}")
            self.run_canprog(cmd)

    def run_canprog(self, cmd):
        self.update_ui_state(True)
        self.progress_bar.setValue(0)
        self.current_worker = CanprogWorker(cmd)
        self.connect_worker(self.current_worker)
        self.current_worker.start()

    def connect_worker(self, worker):
        worker.line_received.connect(self.log)
        worker.progress_signal.connect(self.progress_bar.setValue)
        worker.finished_signal.connect(self.operation_finished)

    def operation_finished(self, code, summary):
        self.update_ui_state(False)
        if code == 0:
            self.lbl_status.setText("Success")
            self.lbl_status.setStyleSheet("color: #3fb950; font-weight: bold;")
        else:
            self.lbl_status.setText("Failed")
            self.lbl_status.setStyleSheet("color: #f85149; font-weight: bold;")
        self.log(f"Operation finished: {summary}")
        self.current_worker = None

    def save_log(self):
        file, _ = QFileDialog.getSaveFileName(self, "Save Log", "", "Text Files (*.txt)")
        if file:
            with open(file, 'w') as f:
                f.write(self.console.toPlainText())
            self.log(f"Log saved to {file}")

    def closeEvent(self, event):
        if self.current_worker and self.current_worker.isRunning():
            reply = QMessageBox.question(self, "Active Operation", "A task is running. Really close?", 
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
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
