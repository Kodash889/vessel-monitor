# proximity_sensor_debugger.py
#
# A simple script to debug the inductive proximity sensor.
# Connect the sensor's signal wire to the IN1 terminal on the
# Automation 2040 W Mini board.

# --- 1. LIBRARY IMPORTS ---
import time
from automation import Automation2040WMini

# --- 2. INITIALIZATION ---
board = Automation2040WMini()

# The IN1 terminal corresponds to input 0 on the board
INPUT_PIN = 0

print("Starting proximity sensor debugger...")
print("Move a metal object in front of the sensor.")
print("The output should change from 0 (no object) to 1 (object detected).")

# --- 3. MAIN LOOP ---
while True:
    # Read the digital value from the input pin (0 or 1)
    value = board.read_input(INPUT_PIN)
    
    # Print the value to the console
    print(f"Sensor value: {value}")

    # Wait for a short duration to make the output readable
    time.sleep(0.5)
