# vessel_monitor_mqtt.py
#
# This script reads RPM from an induction sensor and temperature from a K-Type
# thermocouple and publishes the data to an MQTT broker over Wi-Fi.
# This code is designed for the Pimoroni Automation 2040 W Mini.

# --- 1. LIBRARY IMPORTS ---
import machine
import utime
import time
import network
import json
import uasyncio
import micropython
from umqtt.simple import MQTTClient
from automation import Automation2040WMini
from network_manager import NetworkManager
from ota import OTAUpdater
import config  # Import the configuration file with all settings

# Custom MAX6675 class from the previous working version.
# This handles the software SPI communication (bit-anging).
class MAX6675:
    """
    Driver for the MAX6675 thermocouple amplifier.
    This class uses a bit-anging approach for SPI communication.
    """
    def __init__(self, sck_pin, cs_pin, so_pin):
        self.sck = machine.Pin(sck_pin, machine.Pin.OUT)
        self.cs = machine.Pin(cs_pin, machine.Pin.OUT, value=1)
        self.so = machine.Pin(so_pin, machine.Pin.IN)
        self.sck.value(0)

    def read(self):
        """Reads the raw 16-bit value from the MAX6675."""
        self.cs.value(0) # Select the chip
        time.sleep_us(10)

        # Manually bit-bang the SPI communication
        raw_data = 0
        for i in range(16):
            self.sck.value(1)
            raw_data <<= 1
            if self.so.value():
                raw_data |= 1
            self.sck.value(0)

        self.cs.value(1) # Deselect the chip
        return raw_data

    def read_temp(self, celsius=True):
        """
        Reads the temperature in Celsius or Fahrenheit.
        Returns temperature or an error string if the circuit is open.
        """
        raw_val = self.read()

        # Bit D2 is the OC (Open Circuit) bit
        if raw_val & 0b00000100:
            return "Error: Open Circuit"

        # The first 12 bits are the temperature data
        temperature_bits = raw_val >> 3

        # Convert to Celsius: each bit represents 0.25°C
        temp_celsius = temperature_bits * 0.25

        if celsius:
            return temp_celsius
        else:
            return (temp_celsius * 9.0/5.0) + 32.0


# --- 2. CONFIGURATION & GLOBALS ---
# MQTT broker settings
MQTT_CLIENT_ID = micropython.const(b"pico_w_" + config.MQTT_DEVICE_NAME.encode('utf-8'))
MQTT_TOPIC_DATA = micropython.const(config.MQTT_TOPIC_DATA.format(config.MQTT_DEVICE_NAME).encode('utf-8'))
MQTT_TOPIC_STATUS = micropython.const(config.MQTT_TOPIC_STATUS.format(config.MQTT_DEVICE_NAME).encode('utf-8'))

# Sensor pin definitions
THERMOCOUPLE_SCK_PIN = 0
THERMOCOUPLE_CS_PIN = 1
THERMOCOUPLE_SO_PIN = 2

# RPM calculation variables
pulse_count = 0
pulses_per_revolution = 1

# --- 3. INITIALIZATION & CALLBACKS ---
# Initialize the Automation 2040 W Mini board
board = Automation2040WMini()

# Initialize the MAX6675 thermocouple driver
thermocouple = MAX6675(
    sck_pin=THERMOCOUPLE_SCK_PIN,
    cs_pin=THERMOCOUPLE_CS_PIN,
    so_pin=THERMOCOUPLE_SO_PIN
)

def pulse_irq_handler(pin):
    """Interrupt handler to increment the pulse count."""
    global pulse_count
    pulse_count += 1

# Correctly get the pin object for IN1 (GP26)
rpm_pin = machine.Pin(26, machine.Pin.IN, machine.Pin.PULL_DOWN)
rpm_pin.irq(trigger=machine.Pin.IRQ_RISING, handler=pulse_irq_handler)

def status_handler(mode, status, ip): # noqa: ARG001
    """Callback for NetworkManager to handle connection status changes."""
    status_text = "Connecting..."
    board.conn_led(20)
    if status is not None:
        if status:
            status_text = "Connection successful!"
            board.conn_led(True)
        else:
            status_text = "Connection failed!"
            board.conn_led(False)
    print(status_text)
    print("IP: {}".format(ip))


def connect_mqtt():
    """Connects to the MQTT broker with LED feedback."""
    lwt_message = json.dumps({"status": "disconnected"})
    client = MQTTClient(
        client_id=MQTT_CLIENT_ID,
        server=config.MQTT_BROKER
    )
    client.set_last_will(MQTT_TOPIC_STATUS, lwt_message.encode('utf-8'))
    try:
        client.connect()
        print("Connected to MQTT broker.")
        board.switch_led(1, True)
        return client
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")
        for _ in range(5):
            board.switch_led(1, True)
            utime.sleep(0.1)
            board.switch_led(1, False)
            utime.sleep(0.1)
        board.switch_led(1, False)
        return None

# --- 4. ASYNCHRONOUS TASKS ---
async def publish_sensor_data(client):
    """Asynchronous task to read sensors and publish data."""
    global pulse_count
    last_publish_time = utime.time()

    while True:
        try:
            current_time = utime.time()
            time_elapsed_seconds = current_time - last_publish_time
            
            # The RPM pulse counter is handled by the interrupt handler
            # We only need to check for the publishing interval
            if time_elapsed_seconds >= config.UPDATE_FREQUENCY_SECONDS:
                if time_elapsed_seconds > 0:
                    rpm = (pulse_count / pulses_per_revolution) / (time_elapsed_seconds / 60)
                else:
                    rpm = 0
                
                temperature_celsius = thermocouple.read_temp()
                
                if isinstance(temperature_celsius, (int, float)):
                    temperature_celsius += config.THERMOCOUPLE_CALIBRATION
                
                if isinstance(temperature_celsius, (int, float)):
                    data_payload = {
                        "device": config.MQTT_DEVICE_NAME,
                        "rpm": round(rpm, 2),
                        "temperature_c": round(temperature_celsius, 2)
                    }
                    json_data = json.dumps(data_payload)
                    print(f"Publishing data: {json_data}")
                    client.publish(MQTT_TOPIC_DATA, json_data)
                else:
                    print(f"Temperature sensor error: {temperature_celsius}")
                    error_payload = {
                        "device": config.MQTT_DEVICE_NAME,
                        "error": temperature_celsius
                    }
                    client.publish(MQTT_TOPIC_DATA, json.dumps(error_payload))
                
                micropython.schedule(lambda: setattr(globals(), 'pulse_count', 0), None)
                last_publish_time = current_time
            
            await uasyncio.sleep_ms(100)
            
        except OSError as e:
            print(f"MQTT publish error: {e}. Trying to reconnect...")
            board.switch_led(1, False)
            return
        except Exception as e:
            print(f"An error occurred in publish_sensor_data: {e}")
            await uasyncio.sleep(1)

async def publish_heartbeat(client):
    """Asynchronous task to publish heartbeat messages."""
    while True:
        try:
            heartbeat_message = json.dumps({"status": "connected"})
            client.publish(MQTT_TOPIC_STATUS, heartbeat_message.encode('utf-8'))
            print("Heartbeat sent.")
            await uasyncio.sleep(config.HEARTBEAT_INTERVAL_SECONDS)
        except OSError as e:
            print(f"Heartbeat publish error: {e}. Trying to reconnect...")
            board.switch_led(1, False)
            return
        except Exception as e:
            print(f"An error occurred in publish_heartbeat: {e}")
            await uasyncio.sleep(1)

async def main_async():
    """Main asynchronous loop for the application."""
    while True:
        mqtt_client = connect_mqtt()
        if mqtt_client:
            print("Starting sensor monitor and publishing to MQTT...")
            print(f"Data topic: {MQTT_TOPIC_DATA.decode('utf-8')}")
            print(f"Status topic: {MQTT_TOPIC_STATUS.decode('utf-8')}")
            heartbeat_message = json.dumps({"status": "connected"})
            mqtt_client.publish(MQTT_TOPIC_STATUS, heartbeat_message.encode('utf-8'))
            uasyncio.create_task(publish_sensor_data(mqtt_client))
            uasyncio.create_task(publish_heartbeat(mqtt_client))
            while True:
                await uasyncio.sleep(1)
        else:
            print("Cannot proceed without an MQTT connection. Retrying in 5 seconds.")
            await uasyncio.sleep(5)

if __name__ == "__main__":
    # Add a startup delay to allow time to interrupt the script
    print("Starting in 5 seconds...")
    time.sleep(5)

    # Move Wi-Fi connection logic to before the OTA check
    print("Starting Wi-Fi connection process...")
    network_manager = NetworkManager(config.COUNTRY, status_handler=status_handler)
    try:
        # Use a non-blocking way to connect to Wi-Fi before OTA check
        uasyncio.run(network_manager.client(config.WIFI_SSID, config.WIFI_PASSWORD))
    except Exception as e:
        print(f"Failed to start network manager: {e}")
        
    check_for_ota_update()

    # Restart the main async loop after the OTA check is complete
    try:
        uasyncio.run(main_async())
    except KeyboardInterrupt:
        print("Program stopped.")