import sys
import time
import os
import logging
from datetime import datetime
BOOTLOADER_BITRATE = 125000

from PyQt6.QtWidgets import (
    QApplication, QMessageBox, QWidget, QLabel, QPushButton, QComboBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog,
    QLineEdit, QTextEdit, QProgressBar, QCheckBox, QGroupBox, QFrame,
    QScrollArea
)
from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt
from PyQt6.QtGui import QFont, QColor, QIcon


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
        self.last_progress_percent = 0

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
                self.last_progress_percent = value

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

            protocol_class = protocols.get_protocol_class_by_name('stm32')

            def open_and_connect(reason="initial connection", retry_limit=5):
                nonlocal iface, protocol, connected

                last_error = None

                for attempt in range(1, retry_limit + 1):
                    try:
                        self._emit_log(
                            f"Opening IXXAT bootloader CAN bus @125 kbit/s... "
                            f"{reason}, attempt {attempt}/{retry_limit}"
                        )

                        iface = can.interface.Bus(
                            interface='ixxat',
                            channel=0,
                            bitrate=BOOTLOADER_BITRATE
                        )

                        time.sleep(0.8)

                        protocol = protocol_class(iface)

                        self._emit_log("Connecting to STM32 bootloader...")
                        connect(protocol)

                        connected = True
                        time.sleep(0.5)

                        self._emit_log("STM32 bootloader connected.")
                        return

                    except Exception as ex:
                        last_error = ex
                        self.line_received.emit(
                            f"RAW CONNECT ERROR attempt {attempt}/{retry_limit}: {str(ex)}"
                        )

                        try:
                            if connected and protocol is not None:
                                disconnect(protocol)
                        except Exception:
                            pass

                        connected = False

                        try:
                            if iface is not None:
                                iface.shutdown()
                        except Exception:
                            pass

                        iface = None
                        protocol = None

                        time.sleep(1.5)

                raise last_error

            open_and_connect()

            if self.cancel_flag:
                raise InterruptedError("Cancelled by user")

            if self.operation == "read_info":
                read(protocol, self.address, self.read_size)

            elif self.operation == "write":
                if not self.hex_file or not os.path.exists(self.hex_file):
                    raise FileNotFoundError("HEX file does not exist.")

                datafile = canprog_file.FileManager()

                def recovery_reconnect(reason, retry_limit=5):
                    nonlocal iface, protocol, connected

                    last_error = None

                    for attempt in range(1, retry_limit + 1):
                        try:
                            self._emit_log(
                                f"Recovery reconnect: {reason}, attempt {attempt}/{retry_limit}"
                            )

                            try:
                                if connected and protocol is not None:
                                    disconnect(protocol)
                            except Exception:
                                pass

                            connected = False

                            try:
                                if iface is not None:
                                    iface.shutdown()
                            except Exception:
                                pass

                            iface = None
                            protocol = None

                            time.sleep(2.0)

                            self._emit_log("Re-opening IXXAT bootloader CAN bus @125 kbit/s...")
                            iface = can.interface.Bus(
                                interface="ixxat",
                                channel=0,
                                bitrate=BOOTLOADER_BITRATE
                            )

                            time.sleep(1.0)

                            protocol = protocol_class(iface)

                            self._emit_log("Reconnecting to STM32 bootloader...")
                            connect(protocol)

                            connected = True

                            time.sleep(1.0)

                            self._emit_log("Recovery reconnect successful.")
                            return

                        except Exception as ex:
                            last_error = ex
                            self.line_received.emit(
                                f"RAW RECOVERY CONNECT ERROR attempt {attempt}/{retry_limit}: {str(ex)}"
                            )

                            try:
                                if iface is not None:
                                    iface.shutdown()
                            except Exception:
                                pass

                            connected = False
                            iface = None
                            protocol = None

                            time.sleep(1.5)

                    raise last_error

                if self.erase_enabled:
                    erase(protocol, [])
                    self.progress_signal.emit(15)

                    self._emit_log("Erase completed successfully.")
                    self._emit_log("Waiting 2 seconds before write...")
                    time.sleep(2.0)

                    recovery_reconnect("after erase")

                    self._emit_log("Reconnect successful. Starting write operation...")

                if self.cancel_flag:
                    raise InterruptedError("Cancelled by user")

                datafile.load(self.hex_file, 'hex', self.address)

                WRITE_RETRY_LIMIT = 5

                for segment_address, data in datafile.get_segments():
                    if self.cancel_flag:
                        raise InterruptedError("Cancelled by user")

                    write_success = False

                    for attempt in range(1, WRITE_RETRY_LIMIT + 1):
                        if self.cancel_flag:
                            raise InterruptedError("Cancelled by user")

                        try:
                            self.last_progress_percent = 0

                            self._emit_log(
                                f"Writing segment at 0x{segment_address:08X}, "
                                f"size {len(data)} bytes... attempt {attempt}/{WRITE_RETRY_LIMIT}"
                            )

                            write(protocol, segment_address, data)

                            write_success = True
                            break

                        except Exception as ex:
                            raw_write_error = str(ex)
                            self.line_received.emit(
                                f"RAW WRITE ERROR attempt {attempt}/{WRITE_RETRY_LIMIT}: {raw_write_error}"
                            )

                            if self.last_progress_percent > 0:
                                raise RuntimeError(
                                    "Write failed after progress already started. "
                                    "Do not retry without erase. Please power cycle, erase again, and retry."
                                )

                            if attempt >= WRITE_RETRY_LIMIT:
                                raise

                            self._emit_log(
                                "Write failed at 0%. Reconnecting IXXAT and retrying write..."
                            )

                            recovery_reconnect(
                                f"write failed at 0%, attempt {attempt}/{WRITE_RETRY_LIMIT}"
                            )

                            time.sleep(1.0)

                    if not write_success:
                        raise RuntimeError("Write failed after retry limit.")

                    time.sleep(0.3)

                if self.verify_enabled:
                    self._emit_log("Writing completed successfully.")
                    self._emit_log("Reconnecting before verification...")

                    recovery_reconnect("before verification")

                    VERIFY_RETRY_LIMIT = 5

                    for segment_address, data in datafile.get_segments():
                        if self.cancel_flag:
                            raise InterruptedError("Cancelled by user")

                        verify_success = False

                        for attempt in range(1, VERIFY_RETRY_LIMIT + 1):
                            if self.cancel_flag:
                                raise InterruptedError("Cancelled by user")

                            try:
                                self.last_progress_percent = 0

                                self._emit_log(
                                    f"Verifying segment at 0x{segment_address:08X}, "
                                    f"size {len(data)} bytes... attempt {attempt}/{VERIFY_RETRY_LIMIT}"
                                )

                                verify(protocol, segment_address, data)

                                verify_success = True
                                break

                            except Exception as ex:
                                raw_verify_error = str(ex)

                                self.line_received.emit(
                                    f"RAW VERIFY ERROR attempt {attempt}/{VERIFY_RETRY_LIMIT}: {raw_verify_error}"
                                )

                                if attempt >= VERIFY_RETRY_LIMIT:
                                    raise

                                self._emit_log(
                                    "Verify failed. Reconnecting IXXAT and retrying verification..."
                                )

                                recovery_reconnect(
                                    f"verify failed, attempt {attempt}/{VERIFY_RETRY_LIMIT}"
                                )

                                time.sleep(1.0)

                        if not verify_success:
                            raise RuntimeError("Verify failed after retry limit.")

                        time.sleep(0.3)

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
        self.setWindowTitle("Transcell PH | STM32 CAN Flasher v1.2 (Factory)")
        self.resize(1500, 850)
        self.setMinimumSize(1200, 700)
        
        self.current_worker = None
        self.log_file = None
        self.tx_count = 0
        self.rx_count = 0
        self.ok_count = 0
        self.fail_count = 0
        self.setup_logging()
        self.workers = []
        self.apply_styles()
        self.init_ui()

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
            self.lbl_boot_ver.setStyleSheet("color: #56d84f; font-weight: 900; font-size: 18px;")
        elif "Chip ID:" in message:
            chip = message.split("Chip ID:")[1].strip()
            self.lbl_chip_id.setText(chip)
            self.lbl_chip_id.setStyleSheet("color: #56d84f; font-weight: 900; font-size: 18px;")

        # Phase/status detection
        if "Mass erasing" in message:
            self.lbl_status.setText("🗑️ Erasing Flash...")
            self.lbl_status.setStyleSheet("color: #f9b44a; font-weight: bold; font-size: 16px;")
        elif "Writing memory" in message:
            self.lbl_status.setText("✍️ Writing Firmware...")
            self.lbl_status.setStyleSheet("color: #2d8cff; font-weight: bold; font-size: 16px;")
        elif "Verifying memory" in message:
            self.lbl_status.setText("✓ Verifying Firmware...")
            self.lbl_status.setStyleSheet("color: #a78bfa; font-weight: bold; font-size: 16px;")
        elif "Starting application" in message:
            self.lbl_status.setText("▶️ Starting Application...")
            self.lbl_status.setStyleSheet("color: #56d84f; font-weight: bold; font-size: 16px;")

    def update_stats(self):
        self.lbl_stats.setText(
            f"TX: {self.tx_count} | RX: {self.rx_count} | OK: {self.ok_count} | FAIL: {self.fail_count}"
        )

    def apply_styles(self):
        """Apply the modern HTML-inspired dark theme with enhanced animations and colors."""
        self.setStyleSheet("""
            QWidget {
                background-color: #071018;
                color: #f2f6fb;
                font-family: "Segoe UI", Arial, sans-serif;
            }
            
            QLabel {
                color: #f2f6fb;
                background-color: transparent;
            }
            
            QGroupBox {
                border: 1px solid rgba(120, 150, 180, 0.22);
                border-radius: 12px;
                margin-top: 12px;
                padding-top: 12px;
                padding-left: 12px;
                padding-right: 12px;
                padding-bottom: 12px;
                background: linear-gradient(180deg, rgba(28, 43, 58, 0.88), rgba(13, 24, 35, 0.88));
                color: #f2f6fb;
                font-weight: 700;
                font-size: 18px;
            }
            
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 3px 0 3px;
            }
            
            QPushButton {
                background: linear-gradient(180deg, rgba(44, 60, 78, 0.86), rgba(27, 39, 52, 0.9));
                border: 1px solid rgba(130, 157, 189, 0.34);
                border-radius: 8px;
                padding: 10px 18px;
                color: #eef5ff;
                font-weight: 700;
                font-size: 15px;
                min-height: 46px;
                transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
            }
            
            QPushButton:hover {
                background: linear-gradient(180deg, #4a7fc7, #2d5a9e);
                border: 2px solid #2d8cff;
                box-shadow: 0 0 20px rgba(45, 140, 255, 0.35);
                color: #ffffff;
            }
            
            QPushButton:pressed {
                background: linear-gradient(180deg, #1f4a7a, #132d52);
                border: 2px solid #1f64d4;
            }
            
            QPushButton#btnConnect {
                background: linear-gradient(180deg, rgba(60, 80, 100, 0.9), rgba(40, 55, 75, 0.95));
                border: 1px solid rgba(100, 140, 180, 0.5);
            }
            
            QPushButton#btnConnect:hover {
                background: linear-gradient(180deg, #5a9ef0, #2b7dd9);
                border: 2px solid #58a6ff;
                box-shadow: 0 0 25px rgba(88, 166, 255, 0.4);
            }
            
            QPushButton#btnFlash {
                background: linear-gradient(180deg, #2f8cff, #1f64d4);
                border: 2px solid rgba(45, 140, 255, 0.6);
                color: #ffffff;
                font-weight: 800;
                font-size: 16px;
                min-height: 84px;
                box-shadow: 0 8px 24px rgba(31, 100, 212, 0.25);
            }
            
            QPushButton#btnFlash:hover {
                background: linear-gradient(180deg, #4d9cff, #3075e8);
                border: 2px solid #7cb3ff;
                box-shadow: 0 12px 32px rgba(45, 140, 255, 0.45);
            }
            
            QPushButton#btnFlash:pressed {
                background: linear-gradient(180deg, #1f64d4, #154bb8);
                border: 2px solid #1f64d4;
                box-shadow: inset 0 4px 12px rgba(0, 0, 0, 0.3);
            }
            
            QPushButton#btnRead, QPushButton#btnTest {
                background: linear-gradient(180deg, rgba(50, 70, 90, 0.9), rgba(30, 45, 60, 0.95));
                border: 1px solid rgba(92, 151, 255, 0.3);
            }
            
            QPushButton#btnRead:hover, QPushButton#btnTest:hover {
                background: linear-gradient(180deg, #4a8bcc, #2d5a9e);
                border: 2px solid #4aa3ff;
                box-shadow: 0 0 20px rgba(74, 163, 255, 0.35);
            }
            
            QPushButton#btnRead:pressed, QPushButton#btnTest:pressed {
                background: linear-gradient(180deg, #1f4a7a, #132d52);
                border: 2px solid #2b5aa0;
            }
            
            QPushButton#btnClear, QPushButton#btnSave {
                background: linear-gradient(180deg, rgba(45, 60, 75, 0.8), rgba(30, 40, 55, 0.85));
                border: 1px solid rgba(110, 130, 160, 0.4);
                min-width: 122px;
            }
            
            QPushButton#btnClear:hover, QPushButton#btnSave:hover {
                background: linear-gradient(180deg, #4a7fa0, #2d5270);
                border: 2px solid #6ba3d0;
                box-shadow: 0 0 15px rgba(107, 163, 208, 0.3);
            }
            
            QPushButton:disabled {
                opacity: 0.35;
                background: linear-gradient(180deg, rgba(70, 84, 101, 0.5), rgba(44, 55, 68, 0.5));
                border: 1px solid rgba(130, 157, 189, 0.15);
                color: #666;
                box-shadow: none;
            }
            
            QLineEdit, QComboBox {
                background: rgba(4, 12, 19, 0.48);
                border: 1px solid rgba(127, 154, 185, 0.36);
                border-radius: 8px;
                padding: 8px 14px;
                color: #f2f6fb;
                font-size: 15px;
                height: 43px;
                selection-background-color: rgba(45, 140, 255, 0.3);
                transition: all 0.2s ease;
            }
            
            QLineEdit:focus, QComboBox:focus {
                border: 2px solid #2d8cff;
                background: rgba(4, 12, 19, 0.70);
                box-shadow: 0 0 12px rgba(45, 140, 255, 0.2);
            }
            
            QComboBox::drop-down {
                border: none;
                padding-right: 10px;
            }
            
            QComboBox::down-arrow {
                image: none;
                width: 10px;
                height: 10px;
            }
            
            QTextEdit {
                background: rgba(2, 8, 13, 0.72);
                border: 1px solid rgba(127, 154, 185, 0.23);
                border-radius: 8px;
                color: #d9e2ec;
                font-family: Consolas, "Courier New", monospace;
                font-size: 13px;
                padding: 12px 20px;
            }
            
            QProgressBar {
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 7px;
                background: rgba(255,255,255,0.05);
                height: 28px;
                text-align: center;
                color: white;
                font-weight: 800;
                font-size: 12px;
            }
            
            QProgressBar::chunk {
                background: linear-gradient(90deg, #4fb63b, #5bd249);
                border-radius: 6px;
                box-shadow: 0 0 8px rgba(95, 210, 73, 0.4);
            }
            
            QCheckBox {
                color: #cbd5e1;
                font-size: 14px;
                spacing: 10px;
            }
            
            QCheckBox::indicator {
                width: 20px;
                height: 20px;
                border-radius: 5px;
                border: 1px solid rgba(127, 154, 185, 0.48);
                background: rgba(4, 12, 19, 0.45);
                transition: all 0.15s ease;
            }
            
            QCheckBox::indicator:hover {
                border: 2px solid #2d8cff;
                background: rgba(4, 12, 19, 0.65);
                box-shadow: 0 0 8px rgba(45, 140, 255, 0.25);
            }
            
            QCheckBox::indicator:checked {
                background: linear-gradient(135deg, #2d8cff, #1f64d4);
                border: 2px solid #4aa3ff;
                box-shadow: 0 0 12px rgba(45, 140, 255, 0.4);
            }
            
            QFrame#titleFrame {
                background: rgba(6, 13, 20, 0.72);
                border-bottom: 1px solid rgba(120, 150, 180, 0.22);
            }
            
            QFrame#footerFrame {
                background: rgba(5, 12, 18, 0.75);
                border-top: 1px solid rgba(120, 150, 180, 0.22);
            }
            
            QScrollArea {
                background: transparent;
                border: none;
            }
        """)

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Title bar
        title_frame = QFrame()
        title_frame.setObjectName("titleFrame")
        title_frame.setFixedHeight(54)
        title_layout = QHBoxLayout(title_frame)
        title_layout.setContentsMargins(22, 0, 22, 0)
        title_layout.setSpacing(12)

        title_icon = QLabel("⚙")
        title_icon.setStyleSheet("font-size: 16px; color: #2d8cff; font-weight: bold;")
        title_text = QLabel("Transcell PH | STM32 CAN Flasher v1.2 (Factory)")
        title_text.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
        title_layout.addWidget(title_icon)
        title_layout.addWidget(title_text)
        title_layout.addStretch()

        main_layout.addWidget(title_frame)

        # Content area
        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(18, 18, 20, 14)
        content_layout.setSpacing(20)

        # Left column with scroll
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        left_widget = QWidget()
        left_layout = self.create_left_column()
        left_widget.setLayout(left_layout)
        left_scroll.setWidget(left_widget)
        content_layout.addWidget(left_scroll, 41)

        # Right column
        right_layout = self.create_right_column()
        content_layout.addLayout(right_layout, 59)

        main_layout.addLayout(content_layout, 1)

        # Footer
        footer_frame = QFrame()
        footer_frame.setObjectName("footerFrame")
        footer_frame.setFixedHeight(48)
        footer_layout = QHBoxLayout(footer_frame)
        footer_layout.setContentsMargins(24, 0, 24, 0)

        footer_left = QLabel("⚙ Factory Mode | Ready")
        footer_left.setStyleSheet("color: #c9d1d9; font-size: 14px; font-weight: 600;")

        self.lbl_stats = QLabel("TX: 0 | RX: 0 | OK: 0 | FAIL: 0")
        self.lbl_stats.setStyleSheet("color: #c9d1d9; font-size: 14px; font-weight: 600;")

        footer_layout.addWidget(footer_left)
        footer_layout.addStretch()
        footer_layout.addWidget(self.lbl_stats)

        main_layout.addWidget(footer_frame)

    def create_left_column(self):
        """Create the left column with controls."""
        layout = QVBoxLayout()
        layout.setSpacing(14)

        # Header with product title and online status
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(10, 0, 10, 2)
        header_layout.setSpacing(14)

        product_icon = QLabel("⚡")
        product_icon.setStyleSheet("font-size: 30px; color: #2d8cff;")
        product_title = QLabel("STM32 CAN Bootloader")
        product_title.setFont(QFont("Segoe UI", 26, QFont.Weight.ExtraBold))

        header_layout.addWidget(product_icon)
        header_layout.addWidget(product_title)
        header_layout.addStretch()

        self.lbl_online = QLabel("● ONLINE")
        self.lbl_online.setStyleSheet("color: #56d84f; font-weight: 700; font-size: 14px;")
        header_layout.addWidget(self.lbl_online)

        layout.addLayout(header_layout)

        # CAN Interface
        can_group = QGroupBox("⌘ CAN Interface")
        can_layout = QGridLayout(can_group)
        can_layout.setSpacing(11)

        can_layout.addWidget(QLabel("Interface:"), 0, 0)
        self.cmb_interface = QComboBox()
        self.cmb_interface.addItems(["IXXAT USB-CAN V2"])
        can_layout.addWidget(self.cmb_interface, 0, 1)

        can_layout.addWidget(QLabel("Bitrate:"), 1, 0)
        self.cmb_bitrate = QComboBox()
        self.cmb_bitrate.addItems(["125 kbit/s", "250 kbit/s", "500 kbit/s", "1000 kbit/s"])
        can_layout.addWidget(self.cmb_bitrate, 1, 1)

        self.btn_connect = QPushButton("🔗 Connect")
        self.btn_connect.setObjectName("btnConnect")
        self.btn_connect.clicked.connect(self.connect_can)
        self.btn_connect.setMinimumHeight(50)
        self.btn_connect.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        can_layout.addWidget(self.btn_connect, 0, 2, 2, 1)

        layout.addWidget(can_group)

        # Bootloader Info
        info_group = QGroupBox("ⓘ Bootloader Info")
        info_layout = QGridLayout(info_group)
        info_layout.setSpacing(14)
        info_layout.setVerticalSpacing(18)

        lbl_boot_version_name = QLabel("Bootloader Version:")
        lbl_boot_version_name.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        lbl_boot_version_name.setStyleSheet("color: #d9e2ec;")
        self.lbl_boot_ver = QLabel("Not Read")
        self.lbl_boot_ver.setFont(QFont("Segoe UI", 20, QFont.Weight.ExtraBold))
        self.lbl_boot_ver.setStyleSheet("color: #56d84f; font-weight: 900;")
        info_layout.addWidget(lbl_boot_version_name, 0, 0)
        info_layout.addWidget(self.lbl_boot_ver, 0, 1, Qt.AlignmentFlag.AlignRight)

        lbl_chip_id_name = QLabel("Chip ID:")
        lbl_chip_id_name.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        lbl_chip_id_name.setStyleSheet("color: #d9e2ec;")
        self.lbl_chip_id = QLabel("Unknown")
        self.lbl_chip_id.setFont(QFont("Segoe UI", 20, QFont.Weight.ExtraBold))
        self.lbl_chip_id.setStyleSheet("color: #56d84f; font-weight: 900;")
        info_layout.addWidget(lbl_chip_id_name, 1, 0)
        info_layout.addWidget(self.lbl_chip_id, 1, 1, Qt.AlignmentFlag.AlignRight)

        lbl_bitrate_name = QLabel("Bootloader bitrate:")
        lbl_bitrate_name.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        lbl_bitrate_name.setStyleSheet("color: #d9e2ec;")
        lbl_bitrate_value = QLabel("125 kbit/s")
        lbl_bitrate_value.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        lbl_bitrate_value.setStyleSheet("color: #f9b44a; font-weight: 700;")
        info_layout.addWidget(lbl_bitrate_name, 2, 0)
        info_layout.addWidget(lbl_bitrate_value, 2, 1, Qt.AlignmentFlag.AlignRight)

        layout.addWidget(info_group)

        # Firmware
        fw_group = QGroupBox("▣ Firmware")
        fw_layout = QGridLayout(fw_group)
        fw_layout.setSpacing(11)

        fw_layout.addWidget(QLabel("File:"), 0, 0)
        self.txt_firmware = QLineEdit()
        self.txt_firmware.setPlaceholderText("C:/EDrive-Jackson/Charles_NR/JBOX3_V54_Test.hex")
        self.btn_browse = QPushButton("...")
        self.btn_browse.clicked.connect(self.browse_firmware)
        self.btn_browse.setMaximumWidth(50)
        self.btn_browse.setObjectName("btnBrowse")
        fw_layout.addWidget(self.txt_firmware, 0, 1)
        fw_layout.addWidget(self.btn_browse, 0, 2)

        self.lbl_file_info = QLabel("No file selected")
        self.lbl_file_info.setStyleSheet("color: #9aa8b7; font-size: 12px;")
        fw_layout.addWidget(self.lbl_file_info, 1, 1, 1, 2)

        layout.addWidget(fw_group)

        # Actions
        action_group = QGroupBox("ϟ Actions")
        action_layout = QGridLayout(action_group)
        action_layout.setSpacing(16)

        self.btn_read_info = QPushButton("📄 Read Device Info")
        self.btn_read_info.setObjectName("btnRead")
        self.btn_read_info.clicked.connect(self.read_device_info)
        self.btn_read_info.setMinimumHeight(84)
        self.btn_read_info.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        action_layout.addWidget(self.btn_read_info, 0, 0)

        self.btn_full_flash = QPushButton("ϟ Flash Firmware")
        self.btn_full_flash.setObjectName("btnFlash")
        self.btn_full_flash.clicked.connect(self.flash_firmware)
        self.btn_full_flash.setMinimumHeight(84)
        self.btn_full_flash.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        action_layout.addWidget(self.btn_full_flash, 0, 1)

        self.btn_test_app = QPushButton("〽 Test Application CAN")
        self.btn_test_app.setObjectName("btnTest")
        self.btn_test_app.clicked.connect(self.test_application_can)
        self.btn_test_app.setMinimumHeight(84)
        self.btn_test_app.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        action_layout.addWidget(self.btn_test_app, 0, 2)

        layout.addWidget(action_group)

        self.chk_skip_erase = QCheckBox("Skip erase (only if already blank)")
        layout.addWidget(self.chk_skip_erase)

        # Progress
        prog_group = QGroupBox("◔ Progress")
        prog_layout = QVBoxLayout(prog_group)
        prog_layout.setSpacing(12)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setMinimumHeight(28)
        self.progress_bar.setValue(0)
        prog_layout.addWidget(self.progress_bar)

        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet("font-weight: bold; color: #56d84f; font-size: 16px;")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        prog_layout.addWidget(self.lbl_status)

        layout.addWidget(prog_group)
        layout.addStretch()

        return layout

    def create_right_column(self):
        """Create the right column with console/log."""
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        log_group = QGroupBox("▹_ Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)

        # Log header
        log_header = QHBoxLayout()
        log_header.setContentsMargins(18, 12, 22, 12)
        log_header.setSpacing(22)

        log_title = QLabel("▹_ Log")
        log_title.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))

        auto_scroll_check = QLabel("✓ Auto scroll")
        auto_scroll_check.setStyleSheet("color: #b8c3cf; font-size: 13px; font-weight: 600;")

        log_header.addWidget(log_title)
        log_header.addStretch()
        log_header.addWidget(auto_scroll_check)
        log_header.addWidget(QLabel("▽ Filter"))

        log_layout.addLayout(log_header)

        # Console
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(QFont("Consolas", 12))
        self.console.setPlainText(
            "STM32 CAN Bootloader Programmer v1.2\n"
            "Integrated CANPROG Engine\n"
            "System Ready. Click Connect to detect IXXAT interface."
        )
        log_layout.addWidget(self.console, 1)

        # Log actions
        log_actions = QHBoxLayout()
        log_actions.setContentsMargins(22, 0, 22, 0)
        log_actions.setSpacing(14)

        self.btn_clear = QPushButton("🗑 Clear")
        self.btn_clear.setObjectName("btnClear")
        self.btn_clear.clicked.connect(self.console.clear)
        self.btn_clear.setMinimumWidth(122)
        self.btn_clear.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))

        self.btn_save_log = QPushButton("💾 Save Log")
        self.btn_save_log.setObjectName("btnSave")
        self.btn_save_log.clicked.connect(self.save_log)
        self.btn_save_log.setMinimumWidth(122)
        self.btn_save_log.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))

        log_actions.addWidget(self.btn_clear)
        log_actions.addWidget(self.btn_save_log)
        log_actions.addStretch()

        log_layout.addLayout(log_actions)
        layout.addWidget(log_group, 1)

        return layout

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
            self.lbl_online.setStyleSheet("color: #56d84f; font-weight: 700; font-size: 14px;")
            self.lbl_status.setText("✓ Connected")
            self.lbl_status.setStyleSheet("color: #56d84f; font-weight: bold; font-size: 16px;")
            self.btn_connect.setText("🔗 Disconnect")
            try:
                self.btn_connect.clicked.disconnect()
            except Exception:
                pass
            self.btn_connect.clicked.connect(self.disconnect_can)
        else:
            self.lbl_online.setText("● OFFLINE")
            self.lbl_online.setStyleSheet("color: #ff5c57; font-weight: 700; font-size: 14px;")
            self.lbl_status.setText("Offline")
            self.lbl_status.setStyleSheet("color: #738294; font-size: 16px;")
            self.btn_connect.setText("🔗 Connect")
            try:
                self.btn_connect.clicked.disconnect()
            except Exception:
                pass
            self.btn_connect.clicked.connect(self.connect_can)
        self.update_ui_state(False)

    def disconnect_can(self):
        self.log("CAN interface disconnected")
        self.lbl_boot_ver.setText("Not Read")
        self.lbl_boot_ver.setStyleSheet("color: #f9b44a; font-weight: 900; font-size: 18px;")
        self.lbl_chip_id.setText("Unknown")
        self.lbl_chip_id.setStyleSheet("color: #f9b44a; font-weight: 900; font-size: 18px;")
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
        if self.get_selected_bitrate() != 125000:
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
        if text.startswith("RAW ") or text.startswith("ERROR:"):
            return text

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
            self.lbl_status.setText("✓ Success")
            self.lbl_status.setStyleSheet("color: #56d84f; font-weight: bold; font-size: 16px;")
            self.log(f"✓ Operation successful: {summary}")
        elif code == -2:
            self.lbl_status.setText("⊗ Cancelled")
            self.lbl_status.setStyleSheet("color: #f9b44a; font-weight: bold; font-size: 16px;")
            self.log(f"Operation cancelled: {summary}")
        else:
            self.fail_count += 1
            self.lbl_status.setText("✗ Failed")
            self.lbl_status.setStyleSheet("color: #ff5c57; font-weight: bold; font-size: 16px;")
            self.log(f"✗ Operation failed: {summary}")
        self.update_stats()

    def update_ui_state(self, busy):
        self.btn_connect.setEnabled(not busy)
        self.btn_browse.setEnabled(not busy and self.lbl_online.text() != "● OFFLINE")
        self.btn_read_info.setEnabled(not busy and self.lbl_online.text() != "● OFFLINE")
        self.btn_test_app.setEnabled(not busy and self.lbl_online.text() != "● OFFLINE")
        self.btn_full_flash.setEnabled(not busy and self.lbl_online.text() != "● OFFLINE" and self.txt_firmware.text().lower().endswith(".hex"))
        self.cmb_interface.setEnabled(not busy)
        self.cmb_bitrate.setEnabled(not busy)

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
