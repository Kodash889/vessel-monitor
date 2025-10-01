import math
import time
from automation import Automation2040W, SWITCH_A

"""
Measures the RMS voltage from a CT clamp connected to ADC1.

The script reads a series of samples over time to calculate the RMS (Root Mean Square) voltage.
This is a more accurate way to measure an AC signal compared to a single reading.
"""

# How many times to update the reading per second
UPDATES = 1

# How many readings to take for the RMS calculation
# The CT clamp signal is 50Hz, so we need a minimum of 200 samples to capture two full cycles.
# A higher number of samples will result in a more accurate reading.
SAMPLES_TO_AVERAGE = 500

# The ADC channel your CT clamp is connected to.
ADC_CHANNEL = 1  # ADC1 on the board

# Create a new Automation2040W board object
board = Automation2040W()

# Enable the LED of the switch used to exit the loop
board.switch_led(SWITCH_A, 50)

# Read the ADCs until the user switch is pressed
print("Starting CT Clamp Measurement... Press 'A' to stop.")

while not board.switch_pressed(SWITCH_A):
    
    # Create a list to store voltage readings
    voltages = []

    # Read samples
    for i in range(SAMPLES_TO_AVERAGE):
        # Read the voltage from the specified ADC channel and store it
        voltage = board.read_adc(ADC_CHANNEL)
        voltages.append(voltage)
        
        # A short delay to allow the ADC to settle and get a good sample
        time.sleep(0.001)

    # First, calculate the average (DC offset) of all the readings
    dc_offset = sum(voltages) / SAMPLES_TO_AVERAGE

    # Now, calculate the RMS voltage of the AC component
    # This involves subtracting the DC offset from each sample before squaring
    sum_of_squares = 0
    for voltage in voltages:
        sum_of_squares += (voltage - dc_offset) ** 2

    # Calculate the mean of the squares
    mean_of_squares = sum_of_squares / SAMPLES_TO_AVERAGE
    
    # The RMS voltage is the square root of the mean of the squares
    rms_voltage = math.sqrt(mean_of_squares)
    
    # Print the results
    print(f"DC Offset: {dc_offset:.3f}V, RMS Voltage: {rms_voltage:.3f}V")
    
    time.sleep(1.0 / UPDATES)

# Put the board back into a safe state
board.reset()
