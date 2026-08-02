import adi

# Use the exact USB context string found by iio_info
uri = "usb:20.7.5"

sdr = adi.Pluto(uri=uri)
print("RX Pluto connected successfully!")
print("Sample rate:", sdr.sample_rate)
print("Sample rate:", sdr.sample_rate)