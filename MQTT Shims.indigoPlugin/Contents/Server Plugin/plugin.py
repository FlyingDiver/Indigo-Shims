#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################

import importlib.util
import sys
import os
import logging
import json
import yaml
import pystache
from queue import Queue
from rgbxy import Converter, GamutA, GamutB, GamutC

kCurDevVersCount = 0  # current version of plugin devices


# Indigo really doesn't like dicts with keys that start with a number or symbol...
def safeKey(key):
    if not key[0].isalpha():
        return 'sk' + key
    else:
        return key


################################################################################
class Plugin(indigo.PluginBase):

    ########################################
    # Main Plugin methods
    ########################################
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)

        pfmt = logging.Formatter('%(asctime)s.%(msecs)03d\t[%(levelname)8s] %(name)20s.%(funcName)-25s%(msg)s', datefmt='%Y-%m-%d %H:%M:%S')
        self.plugin_file_handler.setFormatter(pfmt)
        try:
            self.logLevel = int(self.pluginPrefs[u"logLevel"])
        except (Exception,):
            self.logLevel = logging.INFO
        self.indigo_log_handler.setLevel(self.logLevel)
        self.logger.threaddebug(f"logLevel = {str(self.logLevel)}")

        self.triggers = {}
        self.shimDevices = []
        self.decoders = {}
        self.messageTypesWanted = []
        self.messageQueue = Queue()
        self.mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")
        if not self.mqttPlugin.isEnabled():
            self.logger.warning("MQTT Connector plugin not enabled!")

    def startup(self):
        self.logger.info("Starting MQTT Shims")
        indigo.server.subscribeToBroadcast("com.flyingdiver.indigoplugin.mqtt", "com.flyingdiver.indigoplugin.mqtt-message_queued", "message_handler")

    def message_handler(self, notification):
        self.logger.debug(f"message_handler: MQTT message {notification['message_type']} from {indigo.devices[int(notification['brokerID'])].name}")
        self.messageQueue.put(notification)

    def shutdown(self):
        self.logger.info("Shutting down MQTT Shims")

    def deviceStartComm(self, device):
        self.logger.info(f"{device.name}: Starting Device")

        instanceVers = int(device.pluginProps.get('devVersCount', 0))
        if instanceVers >= kCurDevVersCount:
            self.logger.threaddebug(f"{device.name}: Device Version is up to date")
        elif instanceVers < kCurDevVersCount:
            newProps = device.pluginProps
            instanceVers = int(device.pluginProps.get('devVersCount', 0))
            newProps["devVersCount"] = kCurDevVersCount
            device.replacePluginPropsOnServer(newProps)
            device.stateListOrDisplayStateIdChanged()
            self.logger.threaddebug(f"{device.name}: Updated to version {kCurDevVersCount}")
        else:
            self.logger.error(f"{device.name}: Unknown device version: {instanceVers}")

        props = device.pluginProps
        if device.deviceTypeId == 'shimColor':
            props["SupportsColor"] = True
            device.replacePluginPropsOnServer(props)

        assert device.id not in self.shimDevices
        self.shimDevices.append(device.id)
        self.messageTypesWanted.append(device.pluginProps['message_type'])

    def deviceStopComm(self, device):
        self.logger.info(f"{device.name}: Stopping Device")
        assert device.id in self.shimDevices
        self.shimDevices.remove(device.id)
        self.messageTypesWanted.remove(device.pluginProps['message_type'])

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        self.logger.debug("validateDeviceConfigUi, devId={}, typeId={}, valuesDict = {}".format(devId, typeId, valuesDict))

        if typeId == "shimRelay":
            valuesDict["SupportsOnState"] = True
        elif typeId == "shimDimmer":
            valuesDict["SupportsOnState"] = False
            valuesDict["SupportsSensorValue"] = True
        elif typeId == "shimColor":
            valuesDict["SupportsOnState"] = False
            valuesDict["SupportsSensorValue"] = True
            valuesDict["SupportsColor"] = True
            valuesDict["SupportsWhite"] = True
        elif typeId == "shimOnOffSensor":
            valuesDict["SupportsOnState"] = True
            valuesDict["SupportsSensorValue"] = False
        elif typeId == "shimValueSensor":
            valuesDict["SupportsOnState"] = False
            valuesDict["SupportsSensorValue"] = True
        elif typeId == "shimGeneric":
            valuesDict["SupportsOnState"] = False
            valuesDict["SupportsSensorValue"] = False
        return True, valuesDict

    def didDeviceCommPropertyChange(self, oldDevice, newDevice):
        if oldDevice.pluginProps.get('SupportsBatteryLevel') != newDevice.pluginProps.get('SupportsBatteryLevel'):
            return True
        if oldDevice.pluginProps.get('message_type') != newDevice.pluginProps.get('message_type'):
            return True
        if oldDevice.pluginProps.get('custom_decoder') != newDevice.pluginProps.get('custom_decoder'):
            if oldDevice.id in self.decoders:
                del self.decoders[oldDevice.id]
        return False

    def triggerStartProcessing(self, trigger):
        self.logger.debug(f"{trigger.name}: Adding Trigger")
        assert trigger.pluginTypeId in ["deviceUpdated", "stateUpdated"]
        assert trigger.id not in self.triggers
        self.triggers[trigger.id] = trigger

    def triggerStopProcessing(self, trigger):
        self.logger.debug(f"{trigger.name}: Removing Trigger")
        assert trigger.id in self.triggers
        del self.triggers[trigger.id]

    def runConcurrentThread(self):
        try:
            while True:
                self.processMessages()
                self.sleep(0.1)

        except self.StopThread:
            pass

    def processMessages(self):

        while not self.messageQueue.empty():
            notification = self.messageQueue.get()
            if not notification:
                return

            if notification["message_type"] not in self.messageTypesWanted:
                return

            props = {'message_type': notification["message_type"]}
            brokerID = int(notification['brokerID'])
            while True:
                message_data = self.mqttPlugin.executeAction("fetchQueuedMessage", deviceId=brokerID, props=props, waitUntilDone=True)
                if message_data is None:
                    break
                for deviceID in self.shimDevices:
                    device = indigo.devices[deviceID]
                    if device.pluginProps['message_type'] == notification["message_type"]:
                        self.logger.debug(
                            f"{device.name}: processMessages: '{notification['message_type']}' {'/'.join(message_data['topic_parts'])} -> {message_data['payload']}")
                        self.update(device, message_data["topic_parts"], message_data["payload"])

    # Convert a brightness value from the external device-specific value to Indigo scale

    @staticmethod
    def convert_brightness_import(device, brightness):
        scale = device.pluginProps.get("brightness_scale", "100")
        if scale == '255':
            brightness = int(round(100.0 * (brightness / 255.0)))
        return brightness

    # Convert a brightness value from Indigo scale to the external device-specific value

    @staticmethod
    def convert_brightness_export(device, brightness):
        scale = device.pluginProps.get("brightness_scale", "100")
        if scale == '255':
            brightness = int(round(255.0 * (brightness / 100.0)))
        return brightness

    # Convert a color temperature value from the external device-specific value to Indigo scale

    @staticmethod
    def convert_color_temp_import(device, color_temp):
        scale = device.pluginProps.get("color_temp_scale", "Kelvin")
        if scale == "Mirek":
            color_temp = int(round(1000000.0 / color_temp))
        return color_temp

    # Convert a color temperature value from Indigo scale to the external device-specific value

    @staticmethod
    def convert_color_temp_export(device, color_temp):
        scale = device.pluginProps.get("color_temp_scale", "Kelvin")
        if scale == "Mirek":
            color_temp = int(round(1000000.0 / color_temp))
        return color_temp

    # Convert a color space dict from the external device-specific value to Indigo space

    def convert_color_space_import(self, device, color_dict):
        self.logger.debug(f"{device.name}: convert_color_space_import input: {color_dict}")
        space = device.pluginProps.get("color_space", "Indigo")
        if space == "Indigo":
            return color_dict
        else:
            if space == "HueA":
                converter = Converter(GamutA)
            elif space == "HueB":
                converter = Converter(GamutB)
            elif space == "HueC":
                converter = Converter(GamutC)
            else:
                converter = Converter(GamutA)  # default?

            redLevel, greenLevel, blueLevel = converter.xy_to_rgb(color_dict['x'], color_dict['y'])
            self.logger.debug(f"{device.name}: xy_to_rgb output: {redLevel} {greenLevel} {blueLevel}")
            output = {'redLevel': redLevel / 2.55, 'greenLevel': greenLevel / 2.55, 'blueLevel': blueLevel / 2.55}
            self.logger.debug(f"{device.name}: convert_color_space_import output: {output}")
            return output

    # Convert a color space dict from Indigo scale to the external device-specific value

    def convert_color_space_export(self, device, color_dict):
        self.logger.debug(f"{device.name}: convert_color_space_export input: {color_dict}")
        space = device.pluginProps.get("color_space", "Indigo")
        if space == "Indigo":
            return color_dict
        else:
            if space == "HueA":
                converter = Converter(GamutA)
            elif space == "HueB":
                converter = Converter(GamutB)
            elif space == "HueC":
                converter = Converter(GamutC)
            else:
                converter = Converter(GamutA)  # default?

            x, y = converter.rgb_to_xy(2.55 * color_dict['redLevel'], 2.55 * color_dict['greenLevel'], 2.55 * color_dict['blueLevel'])
            self.logger.debug(f"{device.name}: rgb_to_xy output: {x} {y}")
            output = {'x': x, 'y': y}
            self.logger.debug(f"{device.name}: convert_color_space_export output: {output}")
            return output

    def update(self, device, topic_parts, payload):
        state_value = None
        state_key = None
        multi_states_dict = None

        # first determine the UID (address) for this message

        if device.pluginProps.get('uid_location', None) == "topic":
            try:
                topic_field = int(device.pluginProps['uid_location_topic_field'])
            except (Exception,):
                self.logger.error(f"{device.name}: error getting uid_location_topic_field, aborting")
                return
            try:
                uid = topic_parts[topic_field]
            except (Exception,):
                self.logger.error(f"{device.name}: error getting uid value from topic, aborting")
                return

        elif device.pluginProps['uid_location'] == "payload":
            try:
                json_payload = json.loads(payload)
            except (Exception,):
                self.logger.error(f"{device.name}: JSON decode error for uid_location = payload, aborting")
                return
            try:
                uid_location_payload_key = device.pluginProps['uid_location_payload_key']
            except (Exception,):
                self.logger.error(f"{device.name}: error getting uid_location_payload_key, aborting")
                return
            try:
                uid = str(json_payload[uid_location_payload_key])
            except (Exception,):
                self.logger.error(f"{device.name}: error getting uid value from payload, aborting")
                return

        else:
            self.logger.error(f"{device.name}: update can't determine uid location")
            return

        if device.pluginProps['address'].strip() != uid.strip():
            self.logger.debug(f"{device.name}: update uid mismatch: {device.pluginProps['address']} != {uid}")
            return

        # get the JSON payload, if there is one

        try:
            state_data = json.loads(payload)
        except (Exception,):
            state_data = None

        # Determine state (value) location, if any.  Generic Shims don't have a value.

        if device.deviceTypeId == "shimGeneric":
            state_value = None

        elif device.pluginProps.get('state_location', None) == "topic":
            try:
                topic_field = int(device.pluginProps['state_location_topic'])
            except (Exception,):
                self.logger.error(f"{device.name}: error getting state_location_topic")
            else:
                try:
                    state_value = topic_parts[topic_field]
                except (Exception,):
                    self.logger.error(f"{device.name}: error obtaining state value from topic field {topic_field}")
                    state_value = None

        elif (device.pluginProps.get('state_location', None) == "payload") and (device.pluginProps.get('state_location_payload_type', None) == "raw"):
            state_value = payload

        elif (device.pluginProps.get('state_location', None) == "payload") and (
                device.pluginProps.get('state_location_payload_type', None) == "json"):

            if not state_data:
                self.logger.error(f"{device.name}: No JSON payload state_data for state_value")
                return

            if not (state_key := device.pluginProps.get('state_location_payload_key', None)):
                self.logger.error(f"{device.name}: error getting state_location_payload_key")
                return

            try:
                state_value = self.find_key_value(state_key, state_data)
            except (Exception,):
                self.logger.error(f"{device.name}: state_key {state_key} not found in state_data {state_data} aborting")
                return

        else:
            state_value = None

        # these are supported for all devices

        if bool(device.pluginProps.get('SupportsBatteryLevel', False)):
            battery = self.find_key_value(device.pluginProps['battery_payload_key'], state_data)
            device.updateStateOnServer('batteryLevel', battery, uiValue=f'{battery}%')

        if bool(device.pluginProps.get('SupportsEnergyMeter', False)) and ("accumEnergyTotal" in device.states):
            energy = self.find_key_value(device.pluginProps['energy_payload_key'], state_data)
            device.updateStateOnServer('accumEnergyTotal', energy, uiValue=f'{energy} kWh')

        if bool(device.pluginProps.get('SupportsEnergyMeterCurPower', False)) and ("curEnergyLevel" in device.states):
            power = self.find_key_value(device.pluginProps['power_payload_key'], state_data)
            device.updateStateOnServer('curEnergyLevel', power, uiValue=f'{power} W')

        # do multi-states processing, if any
        multi_states_key = device.pluginProps.get('state_dict_payload_key', None)
        self.logger.debug(f"{device.name}: multi_states_key= {multi_states_key}")
        if multi_states_key:
            multi_states_dict = self.find_key_value(multi_states_key, state_data)
            self.logger.debug(f"{device.name}: multi_states_dict = {multi_states_dict}")
            if type(multi_states_dict) is not dict:
                self.logger.error(f"{device.name}: Device config error, bad Multi-States Key value: {multi_states_key}")
                multi_states_dict = None

            if not len(multi_states_dict) > 0:
                self.logger.warning(f"{device.name}: Possible device config error, Multi-States Key {multi_states_key} returns empty dict.")
                multi_states_dict = None

            if multi_states_dict:
                state_updates = []
                old_states = device.pluginProps.get("states_list", indigo.List())
                new_states = indigo.List()
                for key in multi_states_dict:
                    if multi_states_dict[key] is not None:
                        safe_key = safeKey(key)
                        new_states.append(safe_key)
                        self.logger.debug(f"{device.name}: adding to state_updates: {safe_key}, {multi_states_dict[key]}, {type(multi_states_dict[key])}")
                        if type(multi_states_dict[key]) in (int, bool, str):
                            state_updates.append({'key': safe_key, 'value': multi_states_dict[key]})
                        elif type(multi_states_dict[key]) is float:
                            state_updates.append({'key': safe_key, 'value': multi_states_dict[key], 'decimalPlaces': 2})
                        else:
                            state_updates.append({'key': safe_key, 'value': json.dumps(multi_states_dict[key])})

                if set(old_states) != set(new_states):
                    self.logger.threaddebug(f"{device.name}: update, new_states: {new_states}")
                    self.logger.threaddebug(f"{device.name}: update, states_list: {state_updates}")
                    newProps = device.pluginProps
                    newProps["states_list"] = new_states
                    device.replacePluginPropsOnServer(newProps)
                    device.stateListOrDisplayStateIdChanged()
                device.updateStatesOnServer(state_updates)

        # do custom decoder processing, if any

        if not self.decoders.get(device.id):    # if we don't have a decoder for this device, try to import one
            decoder_file = device.pluginProps.get('custom_decoder')
            if decoder_file and decoder_file != '0':
                decoder_name = os.path.basename(decoder_file).split('.')[0]
                self.logger.debug(f"{device.name}: Importing custom decoder {decoder_name} @ '{decoder_file}'")
                try:
                    decoder_spec = importlib.util.spec_from_file_location(decoder_name, decoder_file)
                    module = importlib.util.module_from_spec(decoder_spec)
                    sys.modules[decoder_name] = module
                    decoder_spec.loader.exec_module(module)
                    decoder = getattr(module, decoder_name)
                except (Exception,):
                    self.logger.error(f"{device.name}: Custom decoder {decoder_name} @ '{decoder_file}' import error: {sys.exc_info()[0]}")
                else:
                    self.logger.debug(f"{device.name}: Custom decoder {decoder_name} @ '{decoder_file}' imported successfully")
                    self.decoders[device.id] = decoder(decoder.__name__)

        if decoder := self.decoders.get(device.id):
            self.logger.debug(f"{device.name}: Using cached Custom decoder {decoder.name}")
            try:
                decoder_output = decoder.decode(state_data)
            except Exception as err:
                self.logger.error(f"{device.name}: Decode error: {err}")
                decoder_output = None

            if decoder_output:
                state_updates = []
                new_states = indigo.List()
                for key in decoder_output:
                    safe_key = safeKey(key)
                    new_states.append(safe_key)
                    self.logger.debug(f"{device.name}: adding to state_updates: {safe_key}, {decoder_output[key]}")
                    state_updates.append({'key': safe_key, 'value': decoder_output[key]})

                device = indigo.devices[device.id]  # refresh device object
                old_states = device.pluginProps.get("states_list", indigo.List())
                newProps = device.pluginProps
                newProps["states_list"] = list(old_states) + list(new_states)
                device.replacePluginPropsOnServer(newProps)
                device.stateListOrDisplayStateIdChanged()
                device.updateStatesOnServer(state_updates)

        # Device type specific processing.  No entry for ShimGeneric, it's all handled above

        if state_value is None:
            return

        if device.deviceTypeId in ["shimRelay", "shimOnOffSensor"]:

            if isinstance(state_value, bool):
                isOn = state_value
            elif isinstance(state_value, int):
                isOn = int(state_value)
            else:
                on_value = device.pluginProps.get('state_on_value', None)
                if not on_value:
                    if state_value.lower() in ['off', 'false', '0']:
                        isOn = False
                    else:
                        isOn = True
                else:
                    isOn = (state_value == on_value)

            self.logger.debug(f"{device.name}: Updating state to {isOn}")
            device.updateStateOnServer(key='onOffState', value=isOn)

            if device.pluginProps["shimSensorSubtype"] == "Generic":
                if isOn:
                    device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
                else:
                    device.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

            elif device.pluginProps["shimSensorSubtype"] == "MotionSensor":
                if isOn:
                    device.updateStateImageOnServer(indigo.kStateImageSel.MotionSensorTripped)
                else:
                    device.updateStateImageOnServer(indigo.kStateImageSel.MotionSensor)

            elif device.pluginProps["shimSensorSubtype"] == "Power":
                if isOn:
                    device.updateStateImageOnServer(indigo.kStateImageSel.PowerOn)
                else:
                    device.updateStateImageOnServer(indigo.kStateImageSel.PowerOff)

            elif device.pluginProps["shimSensorSubtype"] == "Light":
                if isOn:
                    device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOn)
                else:
                    device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOff)

        if device.deviceTypeId in ["shimDimmer", "shimColor"]:
            state_updates = []

            value_key = device.pluginProps['value_location_payload_key']
            brightness = self.find_key_value(value_key, state_data)
            self.logger.debug(
                f"{device.name}: shimDimmer, state_key = {state_key}, value_key = {value_key}, state_data = {state_data}, state = {state_value}, brightness = {brightness}")

            if state_value.lower() in ['off', 'false', '0']:
                isOn = False
                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOff)
            else:
                isOn = True
                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOn)
            self.logger.debug(f"{device.name}: Setting onOffState to {isOn}")
            state_updates.append({'key': 'onOffState', 'value': isOn})

            if brightness is not None and isOn:
                brightness = self.convert_brightness_import(device, brightness)
                self.logger.debug(f"{device.name}: Updating brightnessLevel to {brightness}")
                state_updates.append({'key': 'brightnessLevel', 'value': brightness})
            device.updateStatesOnServer(state_updates)

        if device.deviceTypeId == "shimColor":
            state_updates = []

            color_value_key = device.pluginProps['color_value_payload_key']
            color_values = self.find_key_value(color_value_key, state_data)
            if color_values:
                color_values = self.convert_color_space_import(device, color_values)

                self.logger.debug(f"{device.name}: Updating color values to {color_values}")
                state_updates.append({'key': 'redLevel', 'value': color_values['redLevel']})
                state_updates.append({'key': 'greenLevel', 'value': color_values['greenLevel']})
                state_updates.append({'key': 'blueLevel', 'value': color_values['blueLevel']})
                self.logger.debug(f"{device.name}: Updating states: {state_updates}")

            color_temp_key = device.pluginProps['color_temp_payload_key']
            color_temp = self.find_key_value(color_temp_key, state_data)

            if color_temp:
                color_temp = self.convert_color_temp_import(device, color_temp)
                self.logger.debug(f"{device.name}: Updating color temperature to {color_temp}")
                state_updates.append({'key': 'whiteTemperature', 'value': color_temp})
            device.updateStatesOnServer(state_updates)

        if device.deviceTypeId == "shimValueSensor":
            try:
                value = float(state_value)
            except (TypeError, ValueError):
                self.logger.error(f"{device.name}: update() is unable to convert '{state_value}' to float")
                return

            function = device.pluginProps.get("adjustmentFunction", None)
            self.logger.threaddebug(f"{device.name}: update adjustmentFunction: '{function}'")
            if function:
                prohibited = ['indigo', 'requests', 'pyserial', 'oauthlib', 'os', 'logging', 'json', 'yaml', 'pystache', 'Queue']
                if any(x in function for x in prohibited):
                    self.logger.warning(f"{device.name}: Invalid method in adjustmentFunction: '{function}'")
                else:
                    x = value
                    value = eval(function)
            self.logger.debug(f"{device.name}: Updating state to {value}")

            if device.pluginProps["shimSensorSubtype"] == "Generic":
                precision = device.pluginProps.get("shimSensorPrecision", "2")
                device.updateStateImageOnServer(indigo.kStateImageSel.NoImage)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f}')

            elif device.pluginProps["shimSensorSubtype"] == "Temperature-F":
                precision = device.pluginProps.get("shimSensorPrecision", "1")
                device.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensorOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} °F')

            elif device.pluginProps["shimSensorSubtype"] == "Temperature-C":
                precision = device.pluginProps.get("shimSensorPrecision", "1")
                device.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensorOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} °C')

            elif device.pluginProps["shimSensorSubtype"] == "Humidity":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.HumiditySensorOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f}%')

            elif device.pluginProps["shimSensorSubtype"] == "Pressure-inHg":
                precision = device.pluginProps.get("shimSensorPrecision", "2")
                device.updateStateImageOnServer(indigo.kStateImageSel.NoImage)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} inHg')

            elif device.pluginProps["shimSensorSubtype"] == "Pressure-mb":
                precision = device.pluginProps.get("shimSensorPrecision", "2")
                device.updateStateImageOnServer(indigo.kStateImageSel.NoImage)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} ,b')

            elif device.pluginProps["shimSensorSubtype"] == "Power-W":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.EnergyMeterOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} W')

            elif device.pluginProps["shimSensorSubtype"] == "Voltage":
                precision = device.pluginProps.get("shimSensorPrecision", "2")
                device.updateStateImageOnServer(indigo.kStateImageSel.EnergyMeterOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} V')

            elif device.pluginProps["shimSensorSubtype"] == "Current":
                precision = device.pluginProps.get("shimSensorPrecision", "2")
                device.updateStateImageOnServer(indigo.kStateImageSel.EnergyMeterOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} A')

            elif device.pluginProps["shimSensorSubtype"] == "Luminance":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.LightSensorOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} lux')

            elif device.pluginProps["shimSensorSubtype"] == "Luminance%":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.LightSensorOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f}%')

            elif device.pluginProps["shimSensorSubtype"] == "ppm":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.NoImage)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} ppm')

            elif device.pluginProps["shimSensorSubtype"] == "speed-mph":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.NoImage)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} mph')

            elif device.pluginProps["shimSensorSubtype"] == "speed-kph":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.NoImage)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} kph')

            elif device.pluginProps["shimSensorSubtype"] == "quantity-in":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.NoImage)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f}"')

            elif device.pluginProps["shimSensorSubtype"] == "quantity-cm":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.NoImage)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=f'{value:.{precision}f} cm')

            else:
                self.logger.debug(f"{device.name}: update, unknown shimSensorSubtype: {device.pluginProps['shimSensorSubtype']}")

        # Now do any triggers

        for trigger in self.triggers.values():
            if trigger.pluginProps["shimDevice"] == str(device.id):
                if trigger.pluginTypeId == "deviceUpdated":
                    indigo.trigger.execute(trigger)
                elif trigger.pluginTypeId == "stateUpdated":
                    state_name = trigger.pluginProps["deviceState"]
                    if state_name in states_dict:
                        indigo.trigger.execute(trigger)

    def find_key_value(self, key_string, data_dict):
        self.logger.threaddebug(f"find_key_value key_string = '{key_string}', data_dict= {data_dict}")
        try:
            if key_string == '.':
                value = data_dict

            elif '.' not in key_string:
                try:
                    if key_string[0] == '[':
                        new_data = data_dict[int(key_string[1:-1])]
                    else:
                        new_data = data_dict.get(key_string, None)
                except (Exception,):
                    value = None
                else:
                    value = new_data

            else:
                split = key_string.split('.', 1)
                self.logger.threaddebug(f"find_key_value split[0] = {split[0]}, split[1] = {split[1]}")
                try:
                    if split[0][0] == '[':
                        new_data = data_dict[int(split[0][1:-1])]
                    else:
                        new_data = data_dict[split[0]]
                except (Exception,):
                    value = None
                else:
                    value = self.find_key_value(split[1], new_data)
        except Exception as e:
            self.logger.error(f"find_key_value error: {e}")
        else:
            self.logger.threaddebug(f"find_key_value result = {value}")
            return value

    @staticmethod
    def getStateList(filter, valuesDict, typeId, deviceId):
        returnList = list()
        if 'states_list' in valuesDict:
            for topic in valuesDict['states_list']:
                returnList.append(topic)
        return returnList

    def getDeviceStateList(self, device):
        stateList = indigo.PluginBase.getDeviceStateList(self, device)
        add_states = device.pluginProps.get("states_list", indigo.List())
        for key in add_states:
            dynamic_state = self.getDeviceStateDictForStringType(str(key), str(key), str(key))
            stateList.append(dynamic_state)
        self.logger.threaddebug(f"{device.name}: getDeviceStateList returning: {stateList}")
        return stateList

    @staticmethod
    def getBrokerDevices(filter="", valuesDict=None, typeId="", targetId=0):
        retList = []
        devicePlugin = valuesDict.get("devicePlugin", None)
        for dev in indigo.devices.iter():
            if dev.protocol == indigo.kProtocol.Plugin and dev.pluginId == "com.flyingdiver.indigoplugin.mqtt" and dev.deviceTypeId != 'aggregator':
                retList.append((dev.id, dev.name))
        retList.sort(key=lambda tup: tup[1])
        return retList

    def get_decoder_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        locations = ["./Decoders", f"{indigo.server.getInstallFolderPath()}/{self.pluginPrefs.get('decoders_folder', '../Python3-includes/Decoders')}"]
        decoders = {}

        for decoder_dir in locations:

            # iterate through the  decoder directory, make list of names and paths
            # r=root, d=directories, f = files

            for root, d, f in os.walk(decoder_dir):
                for file in f:
                    (base, ext) = os.path.splitext(file)
                    if ext == '.py':
                        self.logger.debug(f"Found Python file {file} in {root}")
                        decoders[base] = os.path.join(root, file)

        retList = [(0, "None")]
        for key in decoders:
            retList.append((decoders[key], key))
        self.logger.debug(f"{retList}")
        return retList

    ########################################
    # Relay / Dimmer Action callback
    ########################################

    def actionControlDevice(self, action, device):

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            action_template = device.pluginProps.get("action_template", None)
            if not action_template:
                self.logger.error(f"{device.name}: actionControlDevice: no action template")
                return

            payload = self.substitute(device.pluginProps.get("on_action_payload", "on"))
            topic = pystache.render(action_template, {'uniqueID': device.address})
            self.publish_topic(device, topic, payload)

        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            action_template = device.pluginProps.get("action_template", None)
            if not action_template:
                self.logger.error(f"{device.name}: actionControlDevice: no action template")
                return

            payload = self.substitute(device.pluginProps.get("off_action_payload", "off"))
            topic = pystache.render(action_template, {'uniqueID': device.address})
            self.publish_topic(device, topic, payload)

        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            action_template = device.pluginProps.get("action_template", None)
            if not action_template:
                self.logger.error(f"{device.name}: actionControlDevice: no action template")
                return

            payload = self.substitute(device.pluginProps.get("toggle_action_payload", "toggle"))
            topic = pystache.render(action_template, {'uniqueID': device.address})
            self.publish_topic(device, topic, payload)

        elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
            action_template = device.pluginProps.get("dimmer_action_template", None)
            if not action_template:
                self.logger.error(f"{device.name}: actionControlDevice: no action template")
                return
            payload_template = self.substitute(device.pluginProps.get("dimmer_action_payload", None))
            if not payload_template:
                self.logger.error(f"{device.name}: actionControlDevice: no payload template")
                return

            payload_data = {'brightness': self.convert_brightness_export(device, action.actionValue)}
            topic = pystache.render(action_template, {'uniqueID': device.address})
            payload = pystache.render(payload_template, payload_data)
            self.publish_topic(device, topic, payload)

        elif action.deviceAction == indigo.kDeviceAction.BrightenBy:

            newBrightness = device.brightness + action.actionValue
            if newBrightness > 100:
                newBrightness = 100

            action_template = device.pluginProps.get("dimmer_action_template", None)
            if not action_template:
                self.logger.error(f"{device.name}: actionControlDevice: no action template")
                return
            payload_template = self.substitute(evice.pluginProps.get("dimmer_action_payload", None))
            if not payload_template:
                self.logger.error(f"{device.name}: actionControlDevice: no payload template")
                return

            payload_data = {'brightness': self.convert_brightness_export(device, newBrightness)}
            topic = pystache.render(action_template, {'uniqueID': device.address})
            payload = pystache.render(payload_template, payload_data)
            self.publish_topic(device, topic, payload)

        elif action.deviceAction == indigo.kDeviceAction.DimBy:
            newBrightness = device.brightness - action.actionValue
            if newBrightness < 0:
                newBrightness = 0

            action_template = device.pluginProps.get("dimmer_action_template", None)
            if not action_template:
                self.logger.error(f"{device.name}: actionControlDevice: no action template")
                return
            payload_template = self.substitute(device.pluginProps.get("dimmer_action_payload", None))
            if not payload_template:
                self.logger.error(f"{device.name}: actionControlDevice: no payload template")
                return

            payload_data = {'brightness': self.convert_brightness_export(device, newBrightness)}
            topic = pystache.render(action_template, {'uniqueID': device.address})
            payload = pystache.render(payload_template, payload_data)
            self.publish_topic(device, topic, payload)

        elif action.deviceAction == indigo.kDeviceAction.SetColorLevels:

            actionColorVals = action.actionValue
            payload_data = {"brightness": self.convert_brightness_export(device, device.brightness)}

            if device.supportsWhiteTemperature and 'whiteTemperature' in action.actionValue:

                action_template = device.pluginProps.get("set_temp_topic", None)
                if not action_template:
                    self.logger.error(f"{device.name}: actionControlDevice: no topic template for setting color temperature")
                    return
                payload_template = self.substitute(device.pluginProps.get("set_temp_template", None))
                if not payload_template:
                    self.logger.error(f"{device.name}: actionControlDevice: no payload template for setting color temperature")
                    return
                payload_data["color_temp"] = self.convert_color_temp_export(device, float(action.actionValue['whiteTemperature']))

            elif device.supportsRGB and 'redLevel' in action.actionValue:

                action_template = device.pluginProps.get("set_rgb_topic", None)
                if not action_template:
                    self.logger.error(f"{device.name}: actionControlDevice: no topic template for setting RGB color")
                    return
                payload_template = self.substitute(device.pluginProps.get("set_rgb_template", None))
                if not payload_template:
                    self.logger.error(f"{device.name}: actionControlDevice: no payload template for setting RGB color")
                    return

                new_colors = self.convert_color_space_export(device, action.actionValue)
                for key in new_colors:
                    payload_data[key] = new_colors[key]

            else:
                self.logger.debug(f"{device.name}: SetColorLevels, unsupported color change")
                return

            # Render and send
            topic = pystache.render(action_template, {'uniqueID': device.address})
            payload = pystache.render(payload_template, payload_data)
            self.publish_topic(device, topic, payload)

        else:
            self.logger.error(f"{device.name}: actionControlDevice: Unsupported action requested: {action.deviceAction}")

    ########################################
    # General Action callback
    ########################################

    def actionControlUniversal(self, action, device):

        action_template = device.pluginProps.get("action_template", None)
        if not action_template:
            self.logger.error(f"{device.name}: actionControlDevice: no action template")
            return
        topic = pystache.render(action_template, {'uniqueID': device.address})

        if action.deviceAction == indigo.kUniversalAction.RequestStatus or action.deviceAction == indigo.kUniversalAction.EnergyUpdate:
            self.logger.debug(f"{device.name}: actionControlUniversal: RequestStatus")
            if not bool(device.pluginProps.get('SupportsStatusRequest', False)):
                self.logger.warning(f"{device.name}: actionControlUniversal: device does not support status requests")
            else:
                action_template = device.pluginProps.get("status_action_template", None)
                if not action_template:
                    self.logger.error(f"{device.name}: actionControlUniversal: no action template")
                    return
                payload = self.substitute(device.pluginProps.get("status_action_payload", ""))
                topic = pystache.render(action_template, {'uniqueID': device.address})
                self.publish_topic(device, topic, payload)
                self.logger.info(f"Sent '{device.name}' Status Request")

        #       elif action.deviceAction == indigo.kUniversalAction.EnergyReset:
        #           self.logger.debug(f"{device.name}: actionControlUniversal: EnergyReset")
        #           dev.updateStateOnServer("accumEnergyTotal", 0.0)

        else:
            self.logger.error(f"{device.name}: actionControlUniversal: Unsupported action requested: {action.deviceAction}")

    def publish_topic(self, device, topic, payload):

        mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")
        if not mqttPlugin.isEnabled():
            self.logger.error("MQTT Connector plugin not enabled, publish_topic aborting.")
            return

        brokerID = int(device.pluginProps['brokerID'])
        props = {
            'topic': topic,
            'payload': payload,
            'qos': 0,
            'retain': 0,
        }
        mqttPlugin.executeAction("publish", deviceId=brokerID, props=props, waitUntilDone=False)
        self.logger.debug(f"{device.name}: publish_topic: {topic} -> {payload}")

    ########################################
    # PluginConfig methods
    ########################################

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if not userCancelled:
            self.logLevel = int(valuesDict.get("logLevel", logging.INFO))
            self.indigo_log_handler.setLevel(self.logLevel)

    ########################################
    # Custom Plugin Action callbacks (defined in Actions.xml)
    ########################################

    def pickDevice(self, filter=None, valuesDict=None, typeId=0, targetId=0):
        retList = []
        for devID in self.shimDevices:
            device = indigo.devices[int(devID)]
            retList.append((device.id, device.name))
        retList.sort(key=lambda tup: tup[1])
        return retList

    def dumpYAML(self, valuesDict, typeId):
        device = indigo.devices[int(valuesDict["deviceID"])]
        template = {'type': device.deviceTypeId}
        props = {}
        for key, value in device.pluginProps.items():
            if isinstance(value, indigo.List):
                continue
            if isinstance(value, indigo.Dict):
                continue
            if key in ['brokerID', 'address']:
                continue
            if key == 'message_type':
                template['message_type'] = value
                continue
            if value in ['', None]:
                continue
            if not value:
                continue

            props[key] = device.pluginProps[key]

        template['props'] = props

        for trigger in indigo.triggers:
            try:
                if trigger.pluginId == 'com.flyingdiver.indigoplugin.mqtt' and trigger.pluginTypeId == 'topicMatch':
                    if trigger.globalProps['com.flyingdiver.indigoplugin.mqtt']['message_type'] == template['message_type']:
                        match_list = []
                        for item in trigger.globalProps['com.flyingdiver.indigoplugin.mqtt']['match_list']:
                            match_list.append(item)
                        template['trigger'] = {
                            'match_list': json.dumps(match_list),
                            'queueMessage': True
                        }
                        break
            except (Exception,):
                pass

        self.logger.info(f"\n{yaml.safe_dump(template, width=120, indent=4, default_flow_style=False)}")
        return True

    def pickDeviceTemplate(self, filter=None, valuesDict=None, typeId=0, targetId=0):

        locations = ["./Templates", f"{indigo.server.getInstallFolderPath()}/{self.pluginPrefs.get('externalTemplates', 'MQTT Shim Templates')}"]
        templates = {}

        for template_dir in locations:

            # iterate through the  template directory, make list of names and paths
            # r=root, d=directories, f = files

            for root, d, f in os.walk(template_dir):
                for file in f:
                    self.logger.debug(f"Found File {file} in {root}")
                    (base, ext) = os.path.splitext(file)
                    if ext == '.yaml':
                        templates[base] = os.path.join(root, file)

        retList = []
        for key in templates:
            retList.append((templates[key], key))
        retList.sort(key=lambda tup: tup[1])
        self.logger.debug(f"{retList}")
        return retList

    def createDeviceFromTemplate(self, valuesDict, typeId):
        self.logger.debug(f"createDeviceFromTemplate, typeId = {typeId}, valuesDict = {valuesDict}")
        with open(valuesDict['deviceTemplatePath'], 'r') as stream:
            template = yaml.safe_load(stream)

        template['props']['brokerID'] = valuesDict['brokerID']
        template['props']['message_type'] = template['message_type']
        try:
            indigo.device.create(indigo.kProtocol.Plugin,
                                 name=f"{template['type']} {valuesDict['address']}",
                                 address=valuesDict['address'],
                                 deviceTypeId=template['type'],
                                 props=template['props'])
        except Exception as e:
            self.logger.error(f"Error calling indigo.device.create(): {e.message}")

        # create a trigger for this device if needed

        if bool(valuesDict['createTrigger']):

            # look to see if there's an existing trigger for this message-type
            for trigger in indigo.triggers:
                try:
                    if trigger.pluginId == 'com.flyingdiver.indigoplugin.mqtt' and trigger.pluginTypeId == 'topicMatch':
                        if trigger.globalProps['com.flyingdiver.indigoplugin.mqtt']['message_type'] == template['message_type']:
                            # found a match, bail out
                            self.logger.debug(f"Skipping trigger creation, existing trigger for message type '{template['message_type']}' found: {trigger.name}")
                            return True
                except (Exception,):
                    pass

            try:
                indigo.pluginEvent.create(
                    name=f"{template['type']} {valuesDict['address']} Trigger",
                    pluginId="com.flyingdiver.indigoplugin.mqtt",
                    pluginTypeId="topicMatch",
                    props={
                        "brokerID": valuesDict['brokerID'],
                        "message_type": template['message_type'],
                        "queueMessage": template['trigger']['queueMessage'],
                        "match_list": json.loads(template['trigger']['match_list'])
                    })
            except Exception as e:
                self.logger.error(f"Error calling indigo.pluginEvent.create(): {e.message}")

        return True
    