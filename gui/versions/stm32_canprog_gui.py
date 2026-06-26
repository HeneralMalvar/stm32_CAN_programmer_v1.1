import sys
import time
import os
import subprocess
from PyQt6.QtWidgets import (
    QApplication, QMessageBox, QWidget, QLabel, QPushButton, QComboBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog,
    QLineEdit, QTextEdit, QProgressBar, QCheckBox, QGroupBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

class AppTestWorker(QThread):
    line_received = pyqtSignal(str)
    finished_signal = pyqtSignal(int)

    def __init__(self, bitrate=250000, duration_seconds=5):
        super().__init__()
        self.bitrate = bitrate
        self.duration_seconds = duration_seconds

    def run(self):
        try:
            import can
            import time

            bus = can.Bus(
                interface='ixxat',
                channel=0,
                bitrate=self.bitrate
            )

            self.line_received.emit(
                f"Application CAN monitor started @{self.bitrate} bit/s..."
            )

            count = 0
            start = time.time()

            while time.time() - start < self.duration_seconds:
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
            self.line_received.emit(
                f"Application test complete. Frames received: {count}"
            )

            if count == 0:
                self.line_received.emit(
                    "ERROR: No application CAN frames received.\n"
                    "Check: application mode, 250 kbit/s bitrate, CANH/CANL wiring, "
                    "termination, common GND, board power, or application CAN configuration."
                )
                self.finished_signal.emit(-1)
            else:
                self.finished_signal.emit(0)

        except Exception as e:
            self.line_received.emit(f"ERROR: {e}")
            self.finished_signal.emit(-1)


class CanprogWorker(QThread):
    line_received = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal(int)

    def __init__(self, cmd):
        super().__init__()
        self.cmd = cmd
        self.current_phase = ""

    def run(self):
        try:
            process = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
            

            for line in process.stdout:
                line = line.rstrip()
                self.line_received.emit(line)

                if "Writing memory" in line:
                    self.current_phase = "write"

                if "Verifying memory" in line:
                    self.current_phase = "verify"

                if "Progress:" in line and "%" in line:
                    try:
                        percent_text = line.split("Progress:")[1].split("%")[0].strip()
                        value = int(percent_text)

                        if self.current_phase == "write":
                            mapped = int(value * 0.7)
                        elif self.current_phase == "verify":
                            mapped = 70 + int(value * 0.3)
                        else:
                            mapped = value

                        self.progress_signal.emit(mapped)

                    except Exception as e:
                        self.line_received.emit(f"Progress parse error: {e}")

            process.wait()
            self.finished_signal.emit(process.returncode)

        except Exception as e:
            self.line_received.emit(f"ERROR: {e}")
            self.finished_signal.emit(-1)


class STM32CanProgGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Transcell PH STM32 CAN Bootloader Programmer")
        self.resize(1500, 850)
        self.setStyleSheet(self.styles())
        self.init_ui()

    def init_ui(self):
        main = QHBoxLayout(self)

        left = QVBoxLayout()
        right = QVBoxLayout()

        main.addLayout(left, 45)
        main.addLayout(right, 55)

        # Connection
        conn = QGroupBox("Connection")
        grid = QGridLayout(conn)

        self.cmbInterface = QComboBox()
        self.cmbInterface.addItems(["IXXAT USB-to-CAN V2"])

        self.cmbBitrate = QComboBox()
        self.cmbBitrate.addItems(["125 kbit/s", "250 kbit/s"])

        self.btnRefresh = QPushButton("Refresh🔄")
        self.btnRefresh.clicked.connect(self.refresh_can)

        self.btnDisconnect = QPushButton("❌  Disconnect")
        self.btnDisconnect.clicked.connect(self.disconnect_can)

        self.lblConnected = QLabel("")
        self.lblConnected.setStyleSheet("color:#32e875;")

        grid.addWidget(QLabel("CAN Interface:"), 0, 0)
        grid.addWidget(self.cmbInterface, 0, 1)
        grid.addWidget(self.btnRefresh, 0, 2)
        grid.addWidget(self.lblConnected, 0, 3)

        grid.addWidget(QLabel("CAN Bitrate:"), 1, 0)
        grid.addWidget(self.cmbBitrate, 1, 1)
        grid.addWidget(self.btnDisconnect, 1, 3)

        left.addWidget(conn)

        # Bootloader info
        info = QGroupBox("Bootloader Info")
        grid = QGridLayout(info)

        self.lblVersion = QLabel("Not Read")
        self.lblVersion.setStyleSheet("color:#ffaa00; font-weight:bold;")

        self.lblChip = QLabel("Unknown")
        self.lblChip.setStyleSheet("color:#ffaa00; font-weight:bold;")

        self.btnReadInfo = QPushButton("ℹ️  Read Info")
        self.btnReadInfo.clicked.connect(self.read_info)

        grid.addWidget(QLabel("Bootloader Version:"), 0, 0)
        grid.addWidget(self.lblVersion, 0, 1)
        grid.addWidget(QLabel("Chip ID:"), 1, 0)
        grid.addWidget(self.lblChip, 1, 1)
        grid.addWidget(QLabel("Protocol:"), 2, 0)
        grid.addWidget(QLabel("CAN"), 2, 1)
        grid.addWidget(self.btnReadInfo, 1, 2)

        left.addWidget(info)

        # Firmware file
        fw = QGroupBox("Firmware File")
        grid = QGridLayout(fw)

        self.txtFile = QLineEdit("C:\\firmware\\app_v1.0.0.hex")
        self.btnBrowse = QPushButton("...")
        self.btnBrowse.clicked.connect(self.browse_file)

        grid.addWidget(self.txtFile, 0, 0, 1, 3)
        grid.addWidget(self.btnBrowse, 0, 3)
        grid.addWidget(QLabel("File Size:"), 1, 0)
        self.lblFileSize = QLabel("No file selected")
        grid.addWidget(self.lblFileSize, 1, 1)
        self.lblFileType = QLabel("Unknown")
        grid.addWidget(self.lblFileType, 1, 3)

        left.addWidget(fw)

        # Actions

        
        actions = QGroupBox("Actions")
        v = QVBoxLayout(actions)

        row = QHBoxLayout()
        # self.btnErase = QPushButton("🗑️  1. Erase")
        # self.btnWrite = QPushButton("📤  2. Write")
        # self.btnVerify = QPushButton("🛡️  3. Verify")
        # self.btnGo = QPushButton("▶  4. Go")

        # row.addWidget(self.btnErase)
        # row.addWidget(self.btnWrite)
        # row.addWidget(self.btnVerify)
        # row.addWidget(self.btnGo)

        self.btnAppTest = QPushButton("🧪 Application Test @ 250k")
        self.btnAppTest.clicked.connect(self.run_application_test)

        self.btnFull = QPushButton("⚡  Full Update (Erase → Write → Verify → Go)")
        self.btnFull.clicked.connect(self.run_full_update)
        self.chkSkipErase = QCheckBox("Skip Erase (use only if area is blank)")

        v.addLayout(row)
        v.addWidget(self.btnAppTest)
        v.addWidget(self.btnFull)
        v.addWidget(self.chkSkipErase)

        left.addWidget(actions)

        # Progress
        prog = QGroupBox("Progress")
        grid = QGridLayout(prog)

        self.progress = QProgressBar()
        self.progress.setValue(0)

        self.lblStep = QLabel("")
        self.lblStep.setStyleSheet("color:#32e875; font-weight:bold;")

        grid.addWidget(QLabel("Overall Progress:"), 0, 0)
        grid.addWidget(QLabel("100%"), 0, 1, alignment=Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.progress, 1, 0, 1, 2)
        grid.addWidget(QLabel("Current Step:"), 2, 0)
        grid.addWidget(self.lblStep, 2, 1)

        left.addWidget(prog)
        left.addStretch()

        # Console
        consoleBox = QGroupBox("Log / Console")
        vbox = QVBoxLayout(consoleBox)

        top = QHBoxLayout()
        top.addStretch()
        top.addWidget(QCheckBox("Auto Scroll"))
        top.addWidget(QCheckBox("Show Frames"))

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setPlainText(
            "STM32 CAN Bootloader Programmer\n"
            "Powered by CANPROG Engine\n"
            "System Ready.\n"
            "Click Refresh to detect IXXAT interface.")

        vbox.addLayout(top)
        vbox.addWidget(self.console)

        right.addWidget(consoleBox)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Ready"))
        bottom.addStretch()
        bottom.addWidget(QLabel("Frames TX: 12580"))
        bottom.addWidget(QLabel("RX: 12579"))
        self.btnClearLog = QPushButton("🧹  Clear Log")
        self.btnClearLog.clicked.connect(self.console.clear)
        bottom.addWidget(self.btnClearLog)

        right.addLayout(bottom)

        self.btnBrowse.setEnabled(False)
        self.btnAppTest.setEnabled(False)
        self.btnFull.setEnabled(False)
        self.btnReadInfo.setEnabled(False)

    def set_flashing_ui(self, flashing):
        self.btnFull.setEnabled(not flashing)
        self.btnBrowse.setEnabled(not flashing)
        self.btnReadInfo.setEnabled(not flashing)
        self.btnAppTest.setEnabled(not flashing)
        self.btnRefresh.setEnabled(not flashing)
        self.btnDisconnect.setEnabled(not flashing)

    def browse_file(self):
        file, _ = QFileDialog.getOpenFileName(
            self,
            "Select HEX File",
            "",
            "HEX Files (*.hex);;BIN Files (*.bin);;All Files (*.*)"
        )

        if not file:
            return

        self.txtFile.setText(file)

        ext = os.path.splitext(file)[1].lower()
        if ext == ".hex":
            self.lblFileType.setText("Intel HEX")
        elif ext == ".bin":
            self.lblFileType.setText("Binary")
        else:
            self.lblFileType.setText(ext.upper() if ext else "Unknown")

        size = os.path.getsize(file)
        size_kb = size / 1024
        self.lblFileSize.setText(f"{size_kb:.2f} KB ({size:,} bytes)")

        self.console.append(f"Selected file: {file}")
        self.console.append(f"File size: {size:,} bytes")

        if ext == ".hex":
            self.btnFull.setEnabled(True)
        else:
            self.btnFull.setEnabled(False)
            self.console.append("Full Update is enabled only for HEX files.")

    #functions

    def read_info(self):
        self._bootloader_error_shown = False
        bitrate_text = self.cmbBitrate.currentText()

        if "250" in bitrate_text:
            QMessageBox.warning(
                self,
                "Invalid Bitrate",
                "Read Info uses STM32 ROM bootloader mode and requires 125 kbit/s.\n\nPlease select 125 kbit/s."
            )
            return

        self.console.append("Reading bootloader information...")

        cmd = ["canprog", "-i", "ixxat", "stm32", "read", "info_test.hex", "-s", "0x10"]

        self.worker = CanprogWorker(cmd)
        
        self.worker.line_received.connect(self.append_clean_log)
        self.worker.finished_signal.connect(
            lambda code: self.console.append("Read info done." if code == 0 else "Read info failed.")
        )
        self.worker.start()
    
    def disconnect_can(self):
        self.lblConnected.setText("●  Disconnected")
        self.lblConnected.setStyleSheet("color:#ff4d4d;")
        self.lblVersion.setText("Not Read")
        self.lblVersion.setStyleSheet("color:#ffaa00; font-weight:bold;")
        self.lblChip.setText("Unkown")
        self.lblChip.setStyleSheet("color:#ffaa00; font-weight:bold;")

        self.progress.setValue(0)

        self.lblStep.setText("Disconnected")
        self.lblStep.setStyleSheet("color:#ff4d4d; font-weight:bold;")

        self.console.append("CAN interface disconnected.")

        self.btnBrowse.setEnabled(False)
        self.btnFull.setEnabled(False)
        self.btnReadInfo.setEnabled(False)
        self.btnAppTest.setEnabled(False)

        self.btnRefresh.setEnabled(True)
        self.btnDisconnect.setEnabled(True)

    def refresh_can(self):
        self.console.append("Detecting IXXAT CAN interface...")

        try:
            import can

            bus = can.Bus(interface='ixxat', channel=0, bitrate=125000)
            bus.shutdown()

            self.lblConnected.setText("●  IXXAT Detected")
            self.lblConnected.setStyleSheet("color:#32e875;")
            self.console.append("IXXAT detected successfully.")
            self.lblStep.setText("Connected")
            self.lblStep.setStyleSheet("color:#32e875;")

            self.btnBrowse.setEnabled(True)
            self.btnReadInfo.setEnabled(True)
            self.btnAppTest.setEnabled(True)

        except Exception as e:
            self.lblConnected.setText("●  Not Detected")
            self.lblConnected.setStyleSheet("color:#ff4d4d;")
            self.console.append("IXXAT detection failed.")
            self.console.append(str(e))


    def append_log(self, text):
        self.console.append(text)

        scrollbar = self.console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

        # Bootloader version
        if "Bootloader version:" in text:
            version = text.split("Bootloader version:")[1].strip()

            self.lblVersion.setText(version)
            self.lblVersion.setStyleSheet("color:#32e875; font-weight:bold;")

        # Chip ID
        elif "Chip ID:" in text:
            chip = text.split("Chip ID:")[1].strip()

            self.lblChip.setText(chip)
            self.lblChip.setStyleSheet("color:#32e875; font-weight:bold;")

        # Phase detection
        if "Mass erasing" in text:
            self.lblStep.setText("Erasing Flash...")
            self.lblStep.setStyleSheet("color:#ffaa00; font-weight:bold;")

        elif "Writing memory" in text:
            self.lblStep.setText("Writing Firmware...")
            self.lblStep.setStyleSheet("color:#00ccff; font-weight:bold;")

        elif "Verifying memory" in text:
            self.lblStep.setText("Verifying Firmware...")
            self.lblStep.setStyleSheet("color:#bb66ff; font-weight:bold;")

        elif "Starting application" in text:
            self.lblStep.setText("Starting Application...")
            self.lblStep.setStyleSheet("color:#32e875; font-weight:bold;")


    def append_clean_log(self, text):

        if hasattr(self, "_bootloader_error_shown") is False:
            self._bootloader_error_shown = False

        cleaned = self.clean_canprog_error(text)

        if cleaned is None:
            return

        if "ERROR: No communication with STM32 Bootloader" in cleaned:

            if self._bootloader_error_shown:
                return

            self._bootloader_error_shown = True

        self.append_log(cleaned)
    def clean_canprog_error(self, text):
        lower = text.lower().strip()

        # Hide low-level CAN spam
        if "can acknowledgment error" in lower:
            return None

        if "can bit error" in lower:
            return None

        if "can bit stuff error" in lower:
            return None

        if "can form error" in lower:
            return None

        if "can message flags" in lower:
            return None

        if "unexpected message info type" in lower:
            return None

        # Hide Python traceback lines
        if "traceback" in lower:
            return None

        if "file " in lower:
            return None

        if "line " in lower:
            return None

        if "sys.exit" in lower:
            return None

        if "connect(protocol)" in lower:
            return None

        if "protocol.connect" in lower:
            return None

        if "self._connect" in lower:
            return None

        if "self._init" in lower:
            return None

        if "self._recv" in lower:
            return None

        if "frame =" in lower:
            return None

        if "msg, already_filtered" in lower:
            return None

        if "return self.bus" in lower:
            return None

        if "~~~~" in text:
            return None

        # Final user-friendly error
        if "error warning limit exceeded" in lower:
            return (
                "ERROR: No communication with STM32 Bootloader.\n"
                "Check:\n"
                "- BOOT0 / BOOT1 switch setting\n"
                "- CAN bitrate (125 kbit/s)\n"
                "- CANH/CANL wiring\n"
                "- 120Ω termination\n"
                "- Board power\n"
                "- CAN bootloader pin routing"
            )

        return text
    def get_selected_bitrate(self):
        if self.cmbBitrate.currentText() == "250 kbit/s":
            return 250000
        return 125000

    def run_application_test(self):
        bitrate = self.get_selected_bitrate()

        if bitrate != 250000:
            QMessageBox.warning(
                self,
                "Invalid Bitrate",
                "Application Test uses application mode and requires 250 kbit/s.\n\nPlease select 250 kbit/s."
            )
            return

        self.console.append("Running application CAN test at 250 kbit/s...")
        self.lblStep.setText("Application Test...")
        self.lblStep.setStyleSheet("color:#00ccff; font-weight:bold;")

        self.worker = AppTestWorker(bitrate=bitrate, duration_seconds=5)
        self.worker.line_received.connect(self.append_clean_log)
        self.worker.finished_signal.connect(self.on_application_test_finished)
        self.set_flashing_ui(True)
        self.worker.start()



    def on_application_test_finished(self, code):
        self.set_flashing_ui(False)

        if code == 0:
            self.lblStep.setText("Application Test Done")
            self.lblStep.setStyleSheet("color:#32e875; font-weight:bold;")
            self.cmbBitrate.setEnabled(True)
            self.console.append("Application test done.")
        else:
            self.lblStep.setText("Application Test Failed")
            self.lblStep.setStyleSheet("color:#ff4d4d; font-weight:bold;")
            self.console.append("Application test failed.")

    def run_full_update(self):
        self._bootloader_error_shown = False
        hex_file = self.txtFile.text().strip()
        if not hex_file:
            self.console.append("ERROR: No HEX file selected.")
            return

        if not hex_file.lower().endswith(".hex"):
            self.console.append("ERROR: Selected file is not a HEX file.")
            return

        if not os.path.exists(hex_file):
            self.console.append("ERROR: HEX file does not exist.")
            return
        
        # interface = "ixxat"

        bitrate_text = self.cmbBitrate.currentText()
        if "250" in bitrate_text:
            QMessageBox.warning(
                self,
                "Invalid Bitrate",
                "Bootloader flashing requires 125 kbit/s.\n\nPlease select 125 kbit/s."
            )
            return

        self.cmbBitrate.setEnabled(False)

        bitrate_map = {
            "125 kbit/s": "125000",
            # "250 kbit/s": "250000",
            # "500 kbit/s": "500000",
            # "1000 kbit/s": "1000000"
        }

        bitrate = bitrate_map.get(bitrate_text, "125000")

        # cmd = [
        #     "canprog",
        #     "-i", "ixxat",
        #     "stm32",
        #     "write",
        #     hex_file,
        #     "-e",
        #     "-v",
        #     "-g"
        # ]

        def get_app_dir():
            if getattr(sys, "frozen", False):
                return os.path.dirname(sys.executable)
            return os.path.dirname(os.path.abspath(__file__))

        base_dir = get_app_dir()

        canprog_path = os.path.join(base_dir, "canprog.exe")

        cmd = [
            canprog_path,
            "-i", "ixxat",
            "stm32",
            "write",
            hex_file,
            "-e",
            "-v",
            "-g"
        ]

        reply = QMessageBox.question(
            self,
            "Confirm Firmware Update",
            "Are you sure you want to flash this firmware to the STM32 device?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            self.console.append("Flash cancelled by user.")
            return

        ##self.console.append("Running: " + " ".join(cmd))
        
        self.lblStep.setText("Flashing...")
        self.lblStep.setStyleSheet("color:#FFD700; font-weight:Bold;")

        self.start_time = time.time()
        self.console.append("Start time: " + time.strftime("%H:%M:%S"))

        self.worker = CanprogWorker(cmd)

        self.worker.line_received.connect(self.append_clean_log)

        self.worker.progress_signal.connect(self.progress.setValue)

        self.worker.finished_signal.connect(self.on_flash_finished)
        self.set_flashing_ui(True)
        self.progress.setValue(0)
        self.worker.start()

    def on_flash_finished(self, code):
        self.set_flashing_ui(False)

        if code == 0:
            self.progress.setValue(100)
            elapsed = time.time() - self.start_time
            self.console.append(f"Duration: {elapsed:.1f} seconds")
            self.lblStep.setText("Done")
            self.lblStep.setStyleSheet("color:#32e875; font-weight:bold;")
            self.console.append("DONE")
            self.cmbBitrate.setEnabled(True)
        else:
            self.lblStep.setText("Failed")
            self.lblStep.setStyleSheet("color:#ff4d4d; font-weight:bold;")
            self.console.append(f"FAILED, exit code {code}")

    def styles(self):
        return """
        QWidget {
            background-color: #101820;
            color: #E8EEF2;
            font-family: Segoe UI;
            font-size: 14px;
        }

        QGroupBox {
            border: 1px solid #3A4652;
            border-radius: 8px;
            margin-top: 12px;
            padding: 12px;
            font-weight: bold;
        }

        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 5px;
        }

        QPushButton {
            background-color: #252F3A;
            border: 1px solid #3D4B58;
            border-radius: 6px;
            padding: 12px;
            color: #FFFFFF;
            font-weight: bold;
        }

        QPushButton:hover {
            background-color: #334155;
        }

        QLineEdit, QComboBox, QTextEdit {
            background-color: #0B1218;
            border: 1px solid #3D4B58;
            border-radius: 5px;
            padding: 8px;
            color: #FFFFFF;
        }

        QTextEdit {
            font-family: Consolas;
            font-size: 13px;
            color: #DDEEFF;
        }

        QProgressBar {
            border: 1px solid #3D4B58;
            border-radius: 5px;
            text-align: center;
            height: 22px;
            background-color: #0B1218;
        }

        QProgressBar::chunk {
            background-color: #21C447;
            border-radius: 5px;
        }

        QCheckBox {
            padding: 5px;
        }
        """


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = STM32CanProgGUI()
    window.show()
    sys.exit(app.exec())