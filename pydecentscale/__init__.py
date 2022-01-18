#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2021 Luca Pinello
# Released under GPLv3
"""
Implementation of the Decent Scale API
see also https://decentespresso.com/decentscale_api
"""

__version__ = "0.1.0"

import asyncio
import binascii
import functools
import logging
import operator
import threading
import time
from itertools import cycle

from bleak import BleakScanner, BleakClient

logger = logging.getLogger(__name__)


FIRMWARE_VERSION = {0xFE: 'v1.0', 0x02: 'v1.1'}


class AsyncioEventLoopThread(threading.Thread):
    def __init__(self, *args, loop=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.loop = asyncio.new_event_loop()
        self.running = False

    def run(self):
        self.running = True
        self.loop.run_forever()

    def run_coro(self, coro,wait_for_result=True):
        
        if wait_for_result:
            return asyncio.run_coroutine_threadsafe(coro, loop=self.loop).result()
        else:
            return asyncio.run_coroutine_threadsafe(coro, loop=self.loop)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        #self.join()
        self.running = False


class DecentScale(AsyncioEventLoopThread):
    
    def __init__(self, *args, timeout=20, fix_dropped_command=True, **kwargs):
        super().__init__(*args, **kwargs)

        self.client = None
        self.timeout=timeout
        self.connected=False
        self.fix_dropped_command=fix_dropped_command
        self.dropped_command_sleep = 0.05  # API Docs says 50ms
        self.weight = None

        self.button_callbacks = []

        #Constants
        self.CHAR_READ='0000FFF4-0000-1000-8000-00805F9B34FB'
        self.CHAR_WRITE='000036f5-0000-1000-8000-00805f9b34fb'
        
        
        #Tare the scale by sending "030FFD000000F1". 
        #Each tare needs to increment the 3rd byte pair, 
        #so you can cycle (for instance) though "030FFE000000F2" "030FFF000000F3" "030F000000000C".
        
        tare_commands=[  bytearray.fromhex(c) for c in ['030F000000000C','030F010000000D','030F020000000E']]
        self.tare_commands=cycle(tare_commands)

        # NOTE: LED_ON is Weight LED, Timer LED, Units (since v1.1)
        # here, we send always both leds on and units == g
        self.led_on_command=bytearray.fromhex('030A0101000009')
        self.led_off_command=bytearray.fromhex('030A0000000009')
        self.start_time_command=bytearray.fromhex('030B030000000B')
        self.stop_time_command=bytearray.fromhex("030B0000000008")
        self.reset_time_command=bytearray.fromhex("030B020000000A" )
        
        super().start()

    def check_connection(func):
        def is_connected(self):
            if self.connected:
                func(self)
            else:
                print("Scale is not connected.")
        return is_connected

    async def _find_address(self):
        
        device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and d.name == 'Decent Scale'
        ,timeout=self.timeout)
        
        if device:
            return device.address
        else:
            print('Error: Scale not found. Trying again...')
    

    async def _connect(self, address):
        
        self.client = BleakClient(address)
        
        if not self.running:
            super().start()
        
        try:
            return await self.client.connect(timeout=self.timeout)
        except Exception as e:
            print('Error:%s\nTrying again...' %e)
            return False   
        
    async def _disconnect(self):
        return await self.client.disconnect()   

    async def __send(self, cmd):
        """Send commands with firmware v1.0 bugfix (resending)"""
        await self.client.write_gatt_char(self.CHAR_WRITE, cmd)
        if self.fix_dropped_command:
            await asyncio.sleep(self.dropped_command_sleep)
            await self.client.write_gatt_char(self.CHAR_WRITE, cmd)

        # Wait 200ms for the command to finish
        # Alternative: receive the notifications and check if the command was acknowledged
        await asyncio.sleep(0.2)

    async def _tare(self):
        await self.__send(next(self.tare_commands))

    async def _led_on(self):
        await self.__send(self.led_on_command)

    async def _led_off(self):
        await self.__send(self.led_off_command)

    async def _start_time(self):
        await self.__send(self.start_time_command)

    async def _stop_time(self):
        await self.__send(self.stop_time_command)

    async def _reset_time(self):
        await self.__send(self.reset_time_command)

    def notification_handler(self, sender, data):
        logger.debug(f"Received Notification at {time.time():.3f}: {binascii.hexlify(data, sep=':').decode('ascii')!r}")

        if data[0] != 0x03 or len(data) != 7:
            # Basic sanity check
            logger.info("Invalid notification: not a Decent Scale?")
            return

        # Calculate XOR
        xor_msg = functools.reduce(operator.xor, data[:-1])
        if xor_msg != data[-1]:
            logger.warning("XOR validation failed for notification")
            return

        # Have to decide by type of the package
        type_ = data[1]

        if type_ in [0xCA, 0xCE]:
            # Weight information
            self.weight = int.from_bytes(data[2:4], byteorder='big', signed=True) / 10
        elif type_ == 0xAA:
            # Button press
            # NOTE: Despite the API documentation saying the XOR field is 0x00, it actually contains the XOR
            logger.debug(f"Button press: {data[2]}, duration: {data[3]}")
            # FIXME: This is not good. it crashes the event loop!
            self._handle_buttons(data[2], data[3])
        elif type_ == 0x0F:
            # tare increment
            pass
        elif type_ == 0x0A:
            # LED on/off -> returns units and battery level
            logger.debug(f"Unit of scale: {'g' if data[3] == 0 else 'oz'}")
            if data[4] <= 0x64:
                logger.debug(f"battery level: {data[4]}%")
            elif data[4] == 0xff:
                logger.debug("Scale is running on USB power")
            else:
                logger.warning(f"Unknown battery level: 0x{data[4]:02x}")
            if data[5] in FIRMWARE_VERSION:
                logger.debug(f"Firmware Version: {FIRMWARE_VERSION[data[5]]}")
            else:
                logger.warning(f"Firmware Version is not known: 0x{data[5]:02x}")
        elif type_ == 0x0B:
            # Timer
            # NOTE: The API documentation says there is a section on "Receiving Timer Info" but this is missing
            pass
        else:
            logger.warning(f"Unknown Notification Type received: 0x{type_:02x}")

    def _handle_buttons(self, button, duration):
        for callback in self.button_callbacks:
            callback(self, button, duration)

    async def _enable_notification(self):
        await self.client.start_notify(self.CHAR_READ, self.notification_handler)
        await asyncio.sleep(1)

    def add_button_handler(self, callback):
        """
        Register a button handler function
        The function must have the following signature:
        function(scale: DecentScale, button: int, duration: int)
        """
        self.button_callbacks.append(callback)

    async def _disable_notification(self):
        await self.client.stop_notify(self.CHAR_READ) 

    @check_connection    
    def enable_notification(self):   
        return self.run_coro(self._enable_notification())
    
    @check_connection 
    def disable_notification(self):   
        self.weight=None
        return self.run_coro(self._disable_notification())
 
    def find_address(self):   
        return self.run_coro(self._find_address())

    
    def connect(self,address):
        if not self.connected:
            self.connected= self.run_coro(self._connect(address))
            
            if self.connected:
                self.led_off()
                self.led_on()
        else:
            print('Already connected.')
    
        return self.connected
                
    def disconnect(self):
        if self.connected:
            self.connected= not self.run_coro(self._disconnect())
        else:
            print('Already disconnected.')
        
        return self.connected
            
    def auto_connect(self,n_retries=3):    
        address = None

        for i in range(n_retries):
            address=self.find_address()
            if address:
                print('Found Decent Scale: %s' % address)
                break
            else:
                print(i)
        
        if address:        
            for i in range(n_retries):
                if self.connect(address):
                    print('Scale connected!')
                    return True
                
        
        print('Autoconnect failed. Make sure the scale is on.')
        return False
    
    @check_connection 
    def tare(self):   
        self.run_coro(self._tare())
        
    @check_connection 
    def start_time(self):   
        self.run_coro(self._start_time())
    
    @check_connection 
    def stop_time(self):   
        self.run_coro(self._stop_time())
                   
    @check_connection 
    def reset_time(self):   
        self.run_coro(self._reset_time())

    @check_connection 
    def led_off(self):   
        self.run_coro(self._led_off())
                   
    @check_connection 
    def led_on(self):   
        self.run_coro(self._led_on())
 

        

