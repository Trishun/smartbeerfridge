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
                self._sum = 0
                for x in range(0, WEIGHT_SAMPLES-1):
                    self._sum += self._events[x]
                self._weight = self._sum/WEIGHT_SAMPLES
                self._measureCnt = 0
                print(str(self._weight) + " kg")
            if not self._measured:
                self._measured = True

    @property
    def weight(self):
        if not self._events:
            return 0
        histogram = collections.Counter(round(num, 1) for num in self._events)
        return histogram.most_common(1)[0][0]


class BoardEvent:
    def __init__(self, topLeft, topRight, bottomLeft, bottomRight, buttonPressed, buttonReleased):

        self.topLeft = topLeft
        self.topRight = topRight
        self.bottomLeft = bottomLeft
        self.bottomRight = bottomRight
        self.buttonPressed = buttonPressed
        self.buttonReleased = buttonReleased
        #convenience value
        self.totalWeight = topLeft + topRight + bottomLeft + bottomRight

class Wiiboard:
    def __init__(self, processor):
        # Sockets and status
        self.receivesocket = None
        self.controlsocket = None

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
            self.receivesocket = bluetooth.BluetoothSocket(bluetooth.L2CAP)
            self.controlsocket = bluetooth.BluetoothSocket(bluetooth.L2CAP)
        except ValueError:
            raise Exception("Error: Bluetooth not found")

    def isConnected(self):
        return self.status == "Connected"

    # Connect to the Wiiboard at bluetooth address <address>
    def connect(self, address):
        if address is None:
            print("Non existant address")
            return
        self.receivesocket.connect((address, 0x13))
        self.controlsocket.connect((address, 0x11))
        if self.receivesocket and self.controlsocket:
            print("Connected to Wiiboard at address " + address)
            self.status = "Connected"
            self.address = address
            print("Wiiboard connected")
            self.calibrate()
            useExt = ["00", COMMAND_REGISTER, "04", "A4", "00", "40", "00"]
            self.send(useExt)
            self.setReportingType()
            print("Wiiboard ready")
        else:
            print("Could not connect to Wiiboard at address " + address)

    def receive(self):
        try:
            while self.status == "Connected" and not self.processor.done:
                data = self.receivesocket.recv(25)
                # print(data)
                # intype = int(data.encode("hex")[2:4])
                intype = int(codecs.encode(data, "hex")[2:4])
                if intype == INPUT_STATUS:
                    # TODO: Status input received. It just tells us battery life really
                    self.setReportingType()
                elif intype == INPUT_READ_DATA:
                    if self.calibrationRequested:
                        # packetLength = (int(str(data[4]).encode("hex"), 16) / 16 + 1)
                        packetLength = data[4] // 16 + 1
                        self.parseCalibrationResponse(data[7:(7 + packetLength)])

                        if packetLength < 16:
                            self.calibrationRequested = False
                            print("Calibration done")
                elif intype == EXTENSION_8BYTES:
                    self.processor.mass(self.createBoardEvent(data[2:12]))
                else:
                    print("ACK to data write received")
        except KeyboardInterrupt:
            self.disconnect()
            sys.exit(0)

    def disconnect(self):
        if self.status == "Connected":
            self.status = "Disconnecting"
            self.receivesocket.close()
            self.controlsocket.close()
            print("\nWiiBoard disconnected")
        self.status = "Disconnected"

    # Try to discover a Wiiboard
    def discover(self):
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

    def createBoardEvent(self, _bytes):
        buttonBytes = _bytes[0:2]
        _bytes = _bytes[2:12]
        buttonPressed = False
        buttonReleased = False

        state = (buttonBytes[0] << 8) | buttonBytes[1]
        if state == BUTTON_DOWN_MASK:
            buttonPressed = True
            if not self.buttonDown:
                print("Button pressed")
                self.buttonDown = True

        if not buttonPressed:
            if self.lastEvent.buttonPressed:
                buttonReleased = True
                self.buttonDown = False
                print("Button released")

        rawTR = (_bytes[0] << 8) + _bytes[1]
        rawBR = (_bytes[2] << 8) + _bytes[3]
        rawTL = (_bytes[4] << 8) + _bytes[5]
        rawBL = (_bytes[6] << 8) + _bytes[7]

        topLeft = self.calcMass(rawTL, TOP_LEFT)
        topRight = self.calcMass(rawTR, TOP_RIGHT)
        bottomLeft = self.calcMass(rawBL, BOTTOM_LEFT)
        bottomRight = self.calcMass(rawBR, BOTTOM_RIGHT)
        boardEvent = BoardEvent(topLeft, topRight, bottomLeft, bottomRight, buttonPressed, buttonReleased)
        return boardEvent

    def calcMass(self, raw, pos):
        val = 0.0
        #calibration[0] is calibration values for 0kg
        #calibration[1] is calibration values for 17kg
        #calibration[2] is calibration values for 34kg
        if raw < self.calibration[0][pos]:
            return val
        elif raw < self.calibration[1][pos]:
            val = 17 * ((raw - self.calibration[0][pos]) / float((self.calibration[1][pos] - self.calibration[0][pos])))
        elif raw > self.calibration[1][pos]:
            val = 17 + 17 * ((raw - self.calibration[1][pos]) / float((self.calibration[2][pos] - self.calibration[1][pos])))

        return val

    def getEvent(self):
        return self.lastEvent

    def getLED(self):
        return self.LED

    def parseCalibrationResponse(self, _bytes):
        index = 0
        if len(_bytes) == 16:
            for i in range(2):
                for j in range(4):
                    self.calibration[i][j] = (_bytes[index] << 8) + _bytes[index + 1]
                    index += 2
        elif len(_bytes) < 16:
            for i in range(4):
                self.calibration[2][i] = (_bytes[index] << 8) + _bytes[index + 1]
                index += 2

    # Send <data> to the Wiiboard
    # <data> should be an array of strings, each string representing a single hex byte
    def send(self, data):
        if self.status != "Connected":
            return
        data[0] = "52"

        senddata = b""
        for byte in data:
            byte = str(byte)
            senddata += codecs.decode(byte, 'hex')

        self.controlsocket.send(senddata)

    #Turns the power button LED on if light is True, off if False
    #The board must be connected in order to set the light
    def setLight(self, light):
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

    def setReportingType(self):
        bytearr = ["00", COMMAND_REPORTING, CONTINUOUS_REPORTING, EXTENSION_8BYTES]
        self.send(bytearr)

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
    board.setLight(False)
    board.wait(500)
    board.setLight(True)
    board.receive()

if __name__ == "__main__":
    main()
