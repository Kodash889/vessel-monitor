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
from umqtt.simple import MQTTClient
from automation import Automation2040WMini
from network_manager import NetworkManager
import config  # Import the configuration file with all settings
import micropython

# Custom MAX6675 class from the previous working version.
# This handles the software SPI communication (bit-anging).
class MAX6675:
    """
    Driver for the MAX6675 thermocouple amplifier.
    This class uses a bit-banging approach for SPI communication.
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


# --- 2. CONFIGURATION ---
# MQTT broker settings
MQTT_CLIENT_ID = micropython.const(b"pico_w_" + config.MQTT_DEVICE_NAME.encode('utf-8'))
MQTT_TOPIC_DATA = micropython.const(config.MQTT_TOPIC_DATA.format(config.MQTT_DEVICE_NAME).encode('utf-8'))
MQTT_TOPIC_STATUS = micropython.const(config.MQTT_TOPIC_STATUS.format(config.MQTT_DEVICE_NAME).encode('utf-8'))

# Sensor pin definitions for Automation 2040 W Mini.
# The IN1 terminal is input 0 on the board.
# The K-type thermocouple is connected to GP0, GP1 and GP2.
THERMOCOUPLE_SCK_PIN = 0  # SPI Clock for the MAX6675. Connect to GP0.
THERMOCOUPLE_CS_PIN = 1   # Chip Select for the MAX6675. Connect to GP1.
THERMOCOUPLE_SO_PIN = 2   # SPI MISO (Serial Out) for the MAX6675. Connect to GP2.

# RPM calculation variables
pulse_count = 0
pulses_per_revolution = 1  # Adjust this based on your sensor and stirrer setup.
last_input_state = 0 # To track the previous state for RPM calculation

# --- 3. INITIALIZATION ---
# Initialize the MAX6675 thermocouple driver
thermocouple = MAX6675(
    sck_pin=THERMOCOUPLE_SCK_PIN,
    cs_pin=THERMOCOUPLE_CS_PIN,
    so_pin=THERMOCOUPLE_SO_PIN
)

# Initialize the Automation 2040 W Mini board
board = Automation2040WMini()

# --- 4. NETWORK & MQTT CONNECTION FUNCTIONS ---
def status_handler(mode, status, ip):  # noqa: ARG001
    """
    A callback function for NetworkManager to handle connection status changes.
    It provides visual feedback using the onboard connection LED.
    """
    status_text = "Connecting..."
    board.conn_led(20)  # Flash LED while connecting
    if status is not None:
        if status:
            status_text = "Connection successful!"
            board.conn_led(True) # Solid LED on success
        else:
            status_text = "Connection failed!"
            board.conn_led(False) # LED off on failure

    print(status_text)
    print("IP: {}".format(ip))


def connect_mqtt():
    """
    Connects to the MQTT broker with LED feedback.
    Returns the MQTTClient object on success, None on failure.
    """
    # The LWT message will be sent if the device disconnects unexpectedly
    lwt_message = json.dumps({"status": "disconnected"})

    client = MQTTClient(
        client_id=MQTT_CLIENT_ID,
        server=config.MQTT_BROKER
    )
    # Set the Last Will and Testament before connecting
    client.set_last_will(MQTT_TOPIC_STATUS, lwt_message.encode('utf-8'))
    
    try:
        client.connect()
        print("Connected to MQTT broker.")
        board.switch_led(1, True) # Keep LED 2 on solid to indicate MQTT connection
        return client
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")
        # Blink rapidly LED 2 to indicate a connection failure
        for _ in range(5):
            board.switch_led(1, True)
            utime.sleep(0.1)
            board.switch_led(1, False)
            utime.sleep(0.1)
        board.switch_led(1, False)
        return None

# --- 5. MAIN LOOP ---
def main():
    global pulse_count
    global last_input_state
    
    # Use NetworkManager to handle the Wi-Fi connection
    print("Starting Wi-Fi connection process...")
    network_manager = NetworkManager(config.COUNTRY, status_handler=status_handler)
    
    try:
        uasyncio.get_event_loop().run_until_complete(network_manager.client(config.WIFI_SSID, config.WIFI_PASSWORD))
    except Exception as e:
        print(f"Failed to start network manager: {e}")
        return

    while not network_manager.isconnected():
        time.sleep(0.1)
    
    mqtt_client = connect_mqtt()
    if not mqtt_client:
        print("Cannot proceed without an MQTT connection. Exiting.")
        return
    
    print("Starting sensor monitor and publishing to MQTT...")
    print(f"Data topic: {MQTT_TOPIC_DATA.decode('utf-8')}")
    print(f"Status topic: {MQTT_TOPIC_STATUS.decode('utf-8')}")
    
    # Publish initial "connected" heartbeat
    heartbeat_message = json.dumps({"status": "connected"})
    mqtt_client.publish(MQTT_TOPIC_STATUS, heartbeat_message.encode('utf-8'))
    last_heartbeat_time = utime.time()
    
    # Initialize the time variables for sensor data publishing
    last_sensor_publish_time = utime.time()

    while True:
        try:
            # --- Read RPM pulses from IN1 (Input 0) ---
            current_input_state = board.read_input(0)
            
            # Check for a rising edge (0 to 1 transition) to count pulses
            if last_input_state == 0 and current_input_state == 1:
                pulse_count += 1

            last_input_state = current_input_state
            
            # --- Publish sensor data if the time interval has passed ---
            if utime.time() - last_sensor_publish_time > config.UPDATE_FREQUENCY_SECONDS:
                # Calculate RPM
                time_elapsed_seconds = utime.time() - last_sensor_publish_time
                rpm = (pulse_count / pulses_per_revolution) / (time_elapsed_seconds / 60)
                
                # Get Temperature data
                temperature_celsius = thermocouple.read_temp()
                # Apply the calibration offset from config.py
                if isinstance(temperature_celsius, (int, float)):
                    temperature_celsius += config.THERMOCOUPLE_CALIBRATION

                # Prepare and Publish Data
                if isinstance(temperature_celsius, (int, float)):
                    # Create a dictionary for the data
                    data_payload = {
                        "device": config.MQTT_DEVICE_NAME,
                        "rpm": round(rpm, 2),
                        "temperature_c": round(temperature_celsius, 2)
                    }
                    # Convert the dictionary to a JSON string
                    json_data = json.dumps(data_payload)
                    
                    print(f"Publishing data: {json_data}")
                    mqtt_client.publish(MQTT_TOPIC_DATA, json_data)
                else:
                    # Handle error case
                    print(f"Temperature sensor error: {temperature_celsius}")
                    error_payload = {
                        "device": config.MQTT_DEVICE_NAME,
                        "error": temperature_celsius
                    }
                    mqtt_client.publish(MQTT_TOPIC_DATA, json.dumps(error_payload))
                    
                # Reset the counter and time for the next interval
                pulse_count = 0
                last_sensor_publish_time = utime.time()

            # --- Publish Heartbeat ---
            if utime.time() - last_heartbeat_time > config.HEARTBEAT_INTERVAL_SECONDS:
                heartbeat_message = json.dumps({"status": "connected"})
                mqtt_client.publish(MQTT_TOPIC_STATUS, heartbeat_message.encode('utf-8'))
                last_heartbeat_time = utime.time()
                print("Heartbeat sent.")
            
            # This small sleep prevents the loop from consuming too much CPU
            utime.sleep_ms(10)
            
        except (OSError, Exception) as e:
            print(f"Connection error or other issue: {e}. Attempting to reconnect...")
            board.switch_led(1, False) # Turn off MQTT LED
            mqtt_client = connect_mqtt()
            if not mqtt_client:
                print("Reconnection failed. Sleeping for 5 seconds...")
                time.sleep(5)
                
if __name__ == "__main__":
    main()
