#!/usr/bin/env python

import collections
import codecs
import time
import bluetooth
import sys
import subprocess

# --------- User Settings ---------
WEIGHT_SAMPLES = 500
# ---------------------------------

# Wiiboard Parameters
CONTINUOUS_REPORTING = "04"  # Easier as string with leading zero
COMMAND_LIGHT = 11
COMMAND_REPORTING = 12
COMMAND_REQUEST_STATUS = 15
COMMAND_REGISTER = 16
COMMAND_READ_REGISTER = 17
INPUT_STATUS = 20
INPUT_READ_DATA = 21
EXTENSION_8BYTES = 32
BUTTON_DOWN_MASK = 8
TOP_RIGHT = 0
BOTTOM_RIGHT = 1
TOP_LEFT = 2
BOTTOM_LEFT = 3
BLUETOOTH_NAME = "Nintendo RVL-WBC-01"


class EventProcessor:
    def __init__(self):
        self._measured = False
        self.done = False
        self._measureCnt = 0
        self._events = [x for x in range(WEIGHT_SAMPLES)]

    def mass(self, event):
        if event.totalWeight > 0:
            self._events[self._measureCnt] = event.totalWeight
            self._measureCnt += 1
            if self._measureCnt == WEIGHT_SAMPLES:
                _sum = 0
                for x in range(0, WEIGHT_SAMPLES - 1):
                    _sum += self._events[x]
                weight = _sum / WEIGHT_SAMPLES
                self._measureCnt = 0
                print(str(weight) + " kg")
            if not self._measured:
                self._measured = True

    @property
    def weight(self):
        if not self._events:
            return 0
        histogram = collections.Counter(round(num, 1) for num in self._events)
        return histogram.most_common(1)[0][0]


class BoardEvent:
    def __init__(self, top_left, top_right, bottom_left, bottom_right, button_pressed, button_released):
        self.topLeft = top_left
        self.topRight = top_right
        self.bottomLeft = bottom_left
        self.bottomRight = bottom_right
        self.buttonPressed = button_pressed
        self.buttonReleased = button_released
        # convenience value
        self.totalWeight = top_left + top_right + bottom_left + bottom_right


class Wiiboard:
    def __init__(self, processor):
        # Sockets and status
        self.receive_socket = None
        self.control_socket = None

        self.processor = processor
        self.calibration = []
        self.calibrationRequested = False
        self.LED = False
        self.address = None
        self.buttonDown = False
        for i in range(3):
            self.calibration.append([])
            for j in range(4):
                self.calibration[i].append(10000)  # high dummy value so events with it don't register

        self.status = "Disconnected"
        self.lastEvent = BoardEvent(0, 0, 0, 0, False, False)

        try:
            self.receive_socket = bluetooth.BluetoothSocket(bluetooth.L2CAP)
            self.control_socket = bluetooth.BluetoothSocket(bluetooth.L2CAP)
        except ValueError:
            raise Exception("Error: Bluetooth not found")

    def is_connected(self):
        return self.status == "Connected"

    def connect(self, address: str):
        """Connect to the Wiiboard at bluetooth MAC address.

        :param address: String representation of MAC address
        """
        if address is None:
            print("Non existant address")
            return
        self.receive_socket.connect((address, 0x13))
        self.control_socket.connect((address, 0x11))
        if self.receive_socket and self.control_socket:
            print("Connected to Wiiboard at address " + address)
            self.status = "Connected"
            self.address = address
            print("Wiiboard connected")
            self.calibrate()
            use_ext = ["00", COMMAND_REGISTER, "04", "A4", "00", "40", "00"]
            self.send(use_ext)
            self.set_reporting_type()
            print("Wiiboard ready")
        else:
            print("Could not connect to Wiiboard at address " + address)

    def receive(self):
        try:
            while self.status == "Connected" and not self.processor.done:
                data = self.receive_socket.recv(25)
                input_type = int(codecs.encode(data, "hex")[2:4])
                if input_type == INPUT_STATUS:
                    # TODO: Status input received. It just tells us battery life really
                    self.set_reporting_type()
                elif input_type == INPUT_READ_DATA:
                    if self.calibrationRequested:
                        packet_length = data[4] // 16 + 1
                        self.parse_calibration_response(data[7:(7 + packet_length)])
                        if packet_length < 16:
                            self.calibrationRequested = False
                            print("Calibration done")
                elif input_type == EXTENSION_8BYTES:
                    self.processor.mass(self.create_board_event(data[2:12]))
                else:
                    print("ACK to data write received")
        except KeyboardInterrupt:
            self.disconnect()
            sys.exit(0)

    def disconnect(self):
        if self.status == "Connected":
            self.status = "Disconnecting"
            self.receive_socket.close()
            self.control_socket.close()
            print("\nWiiBoard disconnected")
        self.status = "Disconnected"

    def discover(self) -> str:
        """Enable Wii Board discovery"""
        print("Press the red sync button on the board now")
        address = None
        bluetoothdevices = bluetooth.discover_devices(duration=6, lookup_names=True)
        for bluetoothdevice in bluetoothdevices:
            if bluetoothdevice[1] == BLUETOOTH_NAME:
                address = bluetoothdevice[0]
                print("Found Wiiboard at address " + address)
        if address is None:
            print("No Wiiboards discovered.")
        return address

    def create_board_event(self, bytes_array):
        button_bytes = bytes_array[0:2]
        bytes_array = bytes_array[2:12]
        button_pressed = False
        button_released = False

        state = (button_bytes[0] << 8) | button_bytes[1]
        if state == BUTTON_DOWN_MASK:
            button_pressed = True
            if not self.buttonDown:
                print("Button pressed")
                self.buttonDown = True

        if not button_pressed:
            if self.lastEvent.buttonPressed:
                button_released = True
                self.buttonDown = False
                print("Button released")

        top_right_raw = (bytes_array[0] << 8) + bytes_array[1]
        bottom_right_raw = (bytes_array[2] << 8) + bytes_array[3]
        top_left_raw = (bytes_array[4] << 8) + bytes_array[5]
        bottom_left_raw = (bytes_array[6] << 8) + bytes_array[7]

        top_left = self.calc_mass(top_left_raw, TOP_LEFT)
        top_right = self.calc_mass(top_right_raw, TOP_RIGHT)
        bottom_left = self.calc_mass(bottom_left_raw, BOTTOM_LEFT)
        bottom_right = self.calc_mass(bottom_right_raw, BOTTOM_RIGHT)
        board_event = BoardEvent(top_left, top_right, bottom_left, bottom_right, button_pressed, button_released)
        return board_event

    def calc_mass(self, raw, pos):
        val = 0.0
        # calibration[0] is calibration values for 0kg
        # calibration[1] is calibration values for 17kg
        # calibration[2] is calibration values for 34kg
        if raw < self.calibration[0][pos]:
            return val
        elif raw < self.calibration[1][pos]:
            val = 17 * ((raw - self.calibration[0][pos]) / float((self.calibration[1][pos] - self.calibration[0][pos])))
        elif raw > self.calibration[1][pos]:
            val = 17 + 17 * ((raw - self.calibration[1][pos]) / float((self.calibration[2][pos] - self.calibration[1][pos])))

        return val

    def get_event(self):
        return self.lastEvent

    def get_led(self):
        return self.LED

    def parse_calibration_response(self, bytes_array: bytearray):
        index = 0
        if len(bytes_array) == 16:
            for i in range(2):
                for j in range(4):
                    self.calibration[i][j] = (bytes_array[index] << 8) + bytes_array[index + 1]
                    index += 2
        elif len(bytes_array) < 16:
            for i in range(4):
                self.calibration[2][i] = (bytes_array[index] << 8) + bytes_array[index + 1]
                index += 2

    def send(self, data: list):
        """Send data to the Wiiboard.

        :param data: an array of strings, each string representing a single hex byte
        """
        if self.status != "Connected":
            return
        data[0] = "52"

        senddata = b""
        for byte in data:
            byte = str(byte)
            senddata += codecs.decode(byte, 'hex')

        self.control_socket.send(senddata)

    def set_light(self, light: bool):
        """Switches the power button LED according to provided value.
        The board must be connected in order to set the light.

        :param light: value if LED should be switched on.
        """
        if light:
            val = "10"
        else:
            val = "00"

        message = ["00", COMMAND_LIGHT, val]
        self.send(message)
        self.LED = light

    def calibrate(self):
        print("Sleep for 5s...")
        self.wait(5000)
        print("Calibrating...")
        message = ["00", COMMAND_READ_REGISTER, "04", "A4", "00", "24", "00", "18"]
        self.send(message)
        self.calibrationRequested = True

    def set_reporting_type(self):
        bytes_array = ["00", COMMAND_REPORTING, CONTINUOUS_REPORTING, EXTENSION_8BYTES]
        self.send(bytes_array)

    def wait(self, millis):
        time.sleep(millis / 1000.0)


def main():
    processor = EventProcessor()

    board = Wiiboard(processor)
    if len(sys.argv) == 1:
        print("Discovering board...")
        address = board.discover()
    else:
        address = sys.argv[1]

    try:
        # Disconnect already-connected devices.
        # This is basically Linux black magic just to get the thing to work.
        subprocess.check_output(["bluez-test-input", "disconnect", address], stderr=subprocess.STDOUT)
        subprocess.check_output(["bluez-test-input", "disconnect", address], stderr=subprocess.STDOUT)
    except:
        pass

    print("Trying to connect...")
    board.connect(address)  # The wii board must be in sync mode at this time
    board.wait(200)
    # Flash the LED so we know we can step on.
    board.set_light(False)
    board.wait(500)
    board.set_light(True)
    board.receive()


if __name__ == "__main__":
    main()
