# noinspection PyUnusedLocal
import os

def main(device, *args, **kwargs):
    device.shell('input tap 600 1000')  # Prevent the device from sleeping
    print('=DONE=')
