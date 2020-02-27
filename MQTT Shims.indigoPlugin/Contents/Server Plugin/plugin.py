#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################

import os
import logging
import json
import yaml
import pystache
from Queue import Queue

kCurDevVersCount = 0        # current version of plugin devices


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
        except:
            self.logLevel = logging.INFO
        self.indigo_log_handler.setLevel(self.logLevel)
        self.logger.threaddebug(u"logLevel = " + str(self.logLevel))


    def startup(self):
        indigo.server.log(u"Starting MQTT Shims")
        self.triggers = {}
        self.shimDevices = []
        self.messageTypesWanted = []
        self.messageQueue = Queue()
        self.mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")
        indigo.server.subscribeToBroadcast(u"com.flyingdiver.indigoplugin.mqtt", u"com.flyingdiver.indigoplugin.mqtt-message_queued", "message_handler")

    def message_handler(self, notification):
        self.logger.debug(u"received notification of MQTT message type {} from {}".format(notification["message_type"], indigo.devices[int(notification["brokerID"])].name))
        self.messageQueue.put(notification)
        

    def shutdown(self):
        indigo.server.log(u"Shutting down MQTT Shims")


    def deviceStartComm(self, device):
        self.logger.info(u"{}: Starting Device".format(device.name))

        instanceVers = int(device.pluginProps.get('devVersCount', 0))
        if instanceVers >= kCurDevVersCount:
            self.logger.threaddebug(u"{}: Device Version is up to date".format(device.name))
        elif instanceVers < kCurDevVersCount:
            newProps = device.pluginProps
            instanceVers = int(device.pluginProps.get('devVersCount', 0))
            newProps["devVersCount"] = kCurDevVersCount
            device.replacePluginPropsOnServer(newProps)
            device.stateListOrDisplayStateIdChanged()
            self.logger.threaddebug(u"{}: Updated to version {}".format(device.name, kCurDevVersCount))
        else:
            self.logger.error(u"{}: Unknown device version: {}".format(device.name. instanceVers))


        assert device.id not in self.shimDevices
        self.shimDevices.append(device.id)
        self.messageTypesWanted.append(device.pluginProps['message_type'])


    def deviceStopComm(self, device):
        self.logger.info(u"{}: Stopping Device".format(device.name))
        assert device.id in self.shimDevices
        self.shimDevices.remove(device.id)
        self.messageTypesWanted.remove(device.pluginProps['message_type'])

    def didDeviceCommPropertyChange(self, oldDevice, newDevice):
        if oldDevice.pluginProps.get('SupportsBatteryLevel', None) != newDevice.pluginProps.get('SupportsBatteryLevel', None):
            return True
        if oldDevice.pluginProps.get('message_type', None) != newDevice.pluginProps.get('message_type', None):
            return True
        return False

    def triggerStartProcessing(self, trigger):
        self.logger.debug("{}: Adding Trigger".format(trigger.name))
        assert trigger.pluginTypeId in ["deviceUpdated", "stateUpdated"]
        assert trigger.id not in self.triggers
        self.triggers[trigger.id] = trigger

    def triggerStopProcessing(self, trigger):
        self.logger.debug("{}: Removing Trigger".format(trigger.name))
        assert trigger.id in self.triggers
        del self.triggers[trigger.id]


    def runConcurrentThread(self):
        try:
            while True:
                if not self.mqttPlugin.isEnabled():
                    self.logger.error(u"processMessages: MQTT Connector plugin not enabled, aborting.")
                    self.sleep(60)
                else:        
                    self.processMessages()                
                    self.sleep(0.1)
                    
        except self.StopThread:
            pass        
            

    def processMessages(self):
    
        while not self.messageQueue.empty():
            notification = self.messageQueue.get()
            if not notification:
                return
            
            if notification["message_type"] in self.messageTypesWanted:
                props = { 'message_type': notification["message_type"] }
                brokerID =  int(notification['brokerID'])
                while True:
                    message_data = self.mqttPlugin.executeAction("fetchQueuedMessage", deviceId=brokerID, props=props, waitUntilDone=True)
                    if message_data == None:
                        break
                    for deviceID in self.shimDevices:
                        device = indigo.devices[deviceID]
                        if device.pluginProps['message_type'] == notification["message_type"]:
                            self.logger.debug(u"{}: processMessages: '{}' {} -> {}".format(device.name, notification["message_type"], '/'.join(message_data["topic_parts"]), message_data["payload"]))
                            self.update(device, message_data)

    
    def update(self, device, message_data):    
        try:
            if device.pluginProps['uid_location'] == "topic":
                message_address = message_data["topic_parts"][int(device.pluginProps['uid_location_topic_field'])]
                self.logger.threaddebug(u"{}: update topic message_address = {}".format(device.name, message_address))
            elif device.pluginProps['uid_location'] == "payload":
                try:
                    payload = json.loads(message_data["payload"])
                except:
                    self.logger.debug(u"{}: JSON decode error for uid_location = payload, aborting".format(device.name))
                    return
                message_address = payload[device.pluginProps['uid_location_payload_key']]
                self.logger.debug(u"{}: update json message_address = {}".format(device.name, message_address))
            else:
                self.logger.debug(u"{}: update can't determine address location".format(device.name))
                return
        except Exception as e:
            self.logger.error(u"{}: Failed to find Unique ID in '{}': {}".format(device.name, device.pluginProps['uid_location'], e))
            return
            
        if device.pluginProps['address'] != message_address:
            self.logger.debug(u"{}: update address mismatch: {} != {}".format(device.name, device.pluginProps['address'], message_address))
            return


        try:
            if device.pluginProps.get('state_location', None) == "topic":
                i = int(device.pluginProps['state_location_topic'])
                self.logger.threaddebug(u"{}: update state_location_topic = {}".format(device.name, i))
                state_key = 'value'
                state_data = { state_key : message_data["topic_parts"][i] }
                
                
            elif (device.pluginProps.get('state_location', None) == "payload") and (device.pluginProps.get('state_location_payload_type', None) == "raw"):
                state_key = 'value'
                state_data = { state_key : message_data["payload"]}

            elif (device.pluginProps.get('state_location', None) == "payload") and (device.pluginProps.get('state_location_payload_type', None) == "json"):
                state_key = device.pluginProps.get('state_location_payload_key', None)
                try:
                    state_data = json.loads(message_data["payload"])
                except:
                    self.logger.debug(u"{}: JSON decode error for state_location = payload, aborting".format(device.name))
                    return
                self.logger.threaddebug(u"{}: update state_location_payload, key = {}".format(device.name, state_key))
                
            else:
                self.logger.debug(u"{}: update can't determine value location".format(device.name))
            
        except Exception as e:
            self.logger.error(u"{}: update error determining message value: {}".format(device.name, e))
            return
        
        if device.deviceTypeId == "shimRelay":
            value = self.recurseDict(state_key, state_data)
            self.logger.debug(u"{}: shimRelay, state_key = {}, state_data = {}, value = {}".format(device.name, state_key, state_data, value))
            if value == None:
                self.logger.debug(u"{}: state_key {} not found in payload".format(device.name, state_key))
                return
            
            on_value = device.pluginProps.get('state_on_value', None)
            if not on_value:
                if value.lower() in ['off', 'false', '0']:
                    value = False
                else:
                    value = True
            else:
                value = (value == on_value)
            self.logger.debug(u"{}: Updating state to {}".format(device.name, value))

            if device.pluginProps["shimSensorSubtype"] == "Generic":
                device.updateStateOnServer(key='onOffState', value=value)
                if value:
                    device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
                else:
                    device.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

            elif device.pluginProps["shimSensorSubtype"] == "MotionSensor":
                device.updateStateOnServer(key='onOffState', value=value)
                if value:
                    device.updateStateImageOnServer(indigo.kStateImageSel.MotionSensorTripped)
                else:
                    device.updateStateImageOnServer(indigo.kStateImageSel.MotionSensor)
                
            elif device.pluginProps["shimSensorSubtype"] == "Power":
                device.updateStateOnServer(key='onOffState', value=value)
                if value:
                    device.updateStateImageOnServer(indigo.kStateImageSel.PowerOn)
                else:
                    device.updateStateImageOnServer(indigo.kStateImageSel.PowerOff)

            if bool(device.pluginProps.get('SupportsBatteryLevel', False)):
                battery = self.recurseDict(device.pluginProps['battery_payload_key'], state_data)
                device.updateStateOnServer('batteryLevel', battery, uiValue='{}%'.format(battery))

            if bool(device.pluginProps.get('SupportsEnergyMeter', False)) and ("accumEnergyTotal" in device.states):
                energy = self.recurseDict(device.pluginProps['energy_payload_key'], state_data)
                device.updateStateOnServer('accumEnergyTotal', energy, uiValue='{} kWh'.format(energy))

            if bool(device.pluginProps.get('SupportsEnergyMeterCurPower', False)) and  ("curEnergyLevel" in device.states):
                power = self.recurseDict(device.pluginProps['power_payload_key'], state_data)
                device.updateStateOnServer('curEnergyLevel', power, uiValue='{} W'.format(power))

            states_key = device.pluginProps.get('state_dict_payload_key', None)
            if not states_key:
                return
            try:
                data = json.loads(message_data["payload"])
            except:
                self.logger.debug(u"{}: JSON decode error for payload, aborting".format(device.name))
                return
            self.logger.threaddebug(u"{}: update state_dict_payload_key, key = {}".format(device.name, states_key))
            states_dict = self.recurseDict(states_key, data)
            if not states_dict:
                return
            elif type(states_dict) != dict:
                self.logger.error(u"{}: Device config error, bad Multi-States Key value: {}".format(device.name, states_key))
                return
            elif not len(states_dict) > 0:
                self.logger.warning(u"{}: Possible device config error, Multi-States Key {} returns empty dict.".format(device.name, states_key))
                return
             
            old_states =  device.pluginProps.get("states_list", indigo.List())
            new_states = indigo.List()                
            states_list = []
            for key in states_dict:
                new_states.append(key)
                states_list.append({'key': key, 'value': states_dict[key], 'decimalPlaces': 2})
            if old_states != new_states:
                self.logger.threaddebug(u"{}: update, new states_list: {}".format(device.name, new_states))
                newProps = device.pluginProps
                newProps["states_list"] = new_states
                device.replacePluginPropsOnServer(newProps)
                device.stateListOrDisplayStateIdChanged()    
            device.updateStatesOnServer(states_list)


        elif device.deviceTypeId == "shimDimmer":
            state = self.recurseDict(state_key, state_data)
            if state == None:
                self.logger.debug(u"{}: state_key {} not found in payload".format(device.name, state_key))
                return 
            self.logger.debug(u"{}: state = {}".format(device.name, state))
            value_key = device.pluginProps['value_location_payload_key']
            value = self.recurseDict(value_key, state_data)
            self.logger.debug(u"{}: shimDimmer, state_key = {}, value_key = {}, data = {}, state = {}, value = {}".format(device.name, state_key, value_key, state_data, state, value))
            if value != None:
                self.logger.debug(u"{}: Updating brightnessLevel to {}".format(device.name, value))
                device.updateStateOnServer(key='brightnessLevel', value=value)
            else:
                if state.lower() in ['off', 'false', '0']:
                    state = False
                else:
                    state = True
                self.logger.debug(u"{}: No brightnessLevel, setting onOffState to {}".format(device.name, state))
                device.updateStateOnServer(key='onOffState', value=state)

            if bool(device.pluginProps.get('SupportsBatteryLevel', False)):
                battery = self.recurseDict(device.pluginProps['battery_payload_key'], state_data)
                device.updateStateOnServer('batteryLevel', battery, uiValue='{}%'.format(battery))

            if bool(device.pluginProps.get('SupportsEnergyMeter', False)) and ("accumEnergyTotal" in device.states):
                energy = self.recurseDict(device.pluginProps['energy_payload_key'], state_data)
                device.updateStateOnServer('accumEnergyTotal', energy, uiValue='{} kWh'.format(energy))

            if bool(device.pluginProps.get('SupportsEnergyMeterCurPower', False)) and  ("curEnergyLevel" in device.states):
                power = self.recurseDict(device.pluginProps['power_payload_key'], state_data)
                device.updateStateOnServer('curEnergyLevel', power, uiValue='{} W'.format(power))


            states_key = device.pluginProps.get('state_dict_payload_key', None)
            if not states_key:
                return
            try:
                data = json.loads(message_data["payload"])
            except:
                self.logger.debug(u"{}: JSON decode error for payload, aborting".format(device.name))
                return
            self.logger.threaddebug(u"{}: update state_dict_payload_key, key = {}".format(device.name, states_key))
            states_dict = self.recurseDict(states_key, data)
            if not states_dict:
                return
            elif type(states_dict) != dict:
                self.logger.error(u"{}: Device config error, bad Multi-States Key value: {}".format(device.name, states_key))
                return
            elif not len(states_dict) > 0:
                self.logger.warning(u"{}: Possible device config error, Multi-States Key {} returns empty dict.".format(device.name, states_key))
                return
             
            old_states =  device.pluginProps.get("states_list", indigo.List())
            new_states = indigo.List()                
            states_list = []
            for key in states_dict:
                new_states.append(key)
                states_list.append({'key': key, 'value': states_dict[key], 'decimalPlaces': 2})
            if old_states != new_states:
                self.logger.threaddebug(u"{}: update, new states_list: {}".format(device.name, new_states))
                newProps = device.pluginProps
                newProps["states_list"] = new_states
                device.replacePluginPropsOnServer(newProps)
                device.stateListOrDisplayStateIdChanged()    
            device.updateStatesOnServer(states_list)

            
        elif device.deviceTypeId == "shimOnOffSensor":
            value = self.recurseDict(state_key, state_data)
            self.logger.debug(u"{}: shimOnOffSensor, state_key = {}, data = {}, value = {}".format(device.name, state_key, state_data, value))

            on_value = device.pluginProps.get('state_on_value', None)
            if not on_value:
                if value in ['off', 'Off', 'OFF', False, '0', 0]:
                    value = False
                else:
                    value = True
            else:
                value = (value == on_value)
            self.logger.debug(u"{}: Updating state to {}".format(device.name, value))

            if device.pluginProps["shimSensorSubtype"] == "Generic":
                device.updateStateOnServer(key='onOffState', value=value)
                if value:
                    device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
                else:
                    device.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

            elif device.pluginProps["shimSensorSubtype"] == "MotionSensor":
                device.updateStateOnServer(key='onOffState', value=value)
                if value:
                    device.updateStateImageOnServer(indigo.kStateImageSel.MotionSensorTripped)
                else:
                    device.updateStateImageOnServer(indigo.kStateImageSel.MotionSensor)
                
            elif device.pluginProps["shimSensorSubtype"] == "Power":
                device.updateStateOnServer(key='onOffState', value=value)
                if value:
                    device.updateStateImageOnServer(indigo.kStateImageSel.PowerOn)
                else:
                    device.updateStateImageOnServer(indigo.kStateImageSel.PowerOff)

            if bool(device.pluginProps.get('SupportsBatteryLevel', False)):
                battery = self.recurseDict(device.pluginProps['battery_payload_key'], state_data)
                device.updateStateOnServer('batteryLevel', battery, uiValue='{}%'.format(battery))

            if bool(device.pluginProps.get('SupportsEnergyMeter', False)) and ("accumEnergyTotal" in device.states):
                energy = self.recurseDict(device.pluginProps['energy_payload_key'], state_data)
                device.updateStateOnServer('accumEnergyTotal', energy, uiValue='{} kWh'.format(energy))

            if bool(device.pluginProps.get('SupportsEnergyMeterCurPower', False)) and  ("curEnergyLevel" in device.states):
                power = self.recurseDict(device.pluginProps['power_payload_key'], state_data)
                device.updateStateOnServer('curEnergyLevel', power, uiValue='{} W'.format(power))

            states_key = device.pluginProps.get('state_dict_payload_key', None)
            if not states_key:
                return
            try:
                data = json.loads(message_data["payload"])
            except:
                self.logger.debug(u"{}: JSON decode error for payload, aborting".format(device.name))
                return
            self.logger.threaddebug(u"{}: update state_dict_payload_key, key = {}".format(device.name, states_key))
            states_dict = self.recurseDict(states_key, data)
            if not states_dict:
                return
            elif type(states_dict) != dict:
                self.logger.error(u"{}: Device config error, bad Multi-States Key value: {}".format(device.name, states_key))
                return
            elif not len(states_dict) > 0:
                self.logger.warning(u"{}: Possible device config error, Multi-States Key {} returns empty dict.".format(device.name, states_key))
                return
             
            old_states =  device.pluginProps.get("states_list", indigo.List())
            new_states = indigo.List()                
            states_list = []
            for key in states_dict:
                new_states.append(key)
                states_list.append({'key': key, 'value': states_dict[key], 'decimalPlaces': 2})
            if old_states != new_states:
                self.logger.threaddebug(u"{}: update, new states_list: {}".format(device.name, new_states))
                newProps = device.pluginProps
                newProps["states_list"] = new_states
                device.replacePluginPropsOnServer(newProps)
                device.stateListOrDisplayStateIdChanged()    
            device.updateStatesOnServer(states_list)

                
        elif device.deviceTypeId == "shimValueSensor":
            value = self.recurseDict(state_key, state_data)
            self.logger.debug(u"{}: shimValueSensor, key = {}, data = {}, value = {}".format(device.name, state_key, state_data, value))
            if value == None:
                self.logger.debug(u"{}: state_key {} not found in payload".format(device.name, state_key))
                return
            try:
                value = float(value)
            except (TypeError, ValueError) as e:
                self.logger.error(u"{}: update unable to convert '{}' to float: {}".format(device.name, value, e))
                return
                           
            function = device.pluginProps.get("adjustmentFunction", None)            
            self.logger.threaddebug(u"{}: update adjustmentFunction: '{}'".format(device.name, function))
            if function:
                prohibited = ['indigo', 'requests', 'pyserial', 'oauthlib']
                if any(x in function for x in prohibited):
                    self.logger.warning(u"{}: Invalid method in adjustmentFunction: '{}'".format(device.name, function))
                else:
                    x = value
                    value = eval(function)

            self.logger.debug(u"{}: Updating state to {}".format(device.name, value))
    
            if device.pluginProps["shimSensorSubtype"] == "Generic":
                precision = device.pluginProps.get("shimSensorPrecision", "2")
                device.updateStateImageOnServer(indigo.kStateImageSel.None)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f}'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Temperature-F":
                precision = device.pluginProps.get("shimSensorPrecision", "1")
                device.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensorOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} °F'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Temperature-C":
                precision = device.pluginProps.get("shimSensorPrecision", "1")
                device.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensorOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} °C'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Humidity":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.HumiditySensorOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f}%'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Pressure-inHg":
                precision = device.pluginProps.get("shimSensorPrecision", "2")
                device.updateStateImageOnServer(indigo.kStateImageSel.None)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} inHg'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Pressure-mb":
                precision = device.pluginProps.get("shimSensorPrecision", "2")
                device.updateStateImageOnServer(indigo.kStateImageSel.None)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} mb'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Power-W":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.EnergyMeterOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} W'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Luminance":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.LightSensorOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} lux'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Luminance%":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.LightSensorOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f}%'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "ppm":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.None)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} ppm'.format(value, prec=precision))


            else:
                self.logger.debug(u"{}: update, unknown shimSensorSubtype: {}".format(device.name, device.pluginProps["shimSensorSubtype"]))

            if bool(device.pluginProps.get('SupportsBatteryLevel', False)):
                battery = self.recurseDict(device.pluginProps['battery_payload_key'], state_data)
                device.updateStateOnServer('batteryLevel', battery, uiValue='{}%'.format(battery))

            if bool(device.pluginProps.get('SupportsEnergyMeter', False)) and ("accumEnergyTotal" in device.states):
                energy = self.recurseDict(device.pluginProps['energy_payload_key'], state_data)
                device.updateStateOnServer('accumEnergyTotal', energy, uiValue='{} kWh'.format(energy))

            if bool(device.pluginProps.get('SupportsEnergyMeterCurPower', False)) and  ("curEnergyLevel" in device.states):
                power = self.recurseDict(device.pluginProps['power_payload_key'], state_data)
                device.updateStateOnServer('curEnergyLevel', power, uiValue='{} W'.format(power))

            states_key = device.pluginProps.get('state_dict_payload_key', None)
            if not states_key:
                return
            try:
                data = json.loads(message_data["payload"])
            except:
                self.logger.debug(u"{}: JSON decode error for payload, aborting".format(device.name))
                return
            self.logger.threaddebug(u"{}: update state_dict_payload_key, key = {}".format(device.name, states_key))
            states_dict = self.recurseDict(states_key, data)
            if not states_dict:
                return
            elif type(states_dict) != dict:
                self.logger.error(u"{}: Device config error, bad Multi-States Key value: {}".format(device.name, states_key))
                return
            elif not len(states_dict) > 0:
                self.logger.warning(u"{}: Possible device config error, Multi-States Key {} returns empty dict.".format(device.name, states_key))
                return
             
            old_states =  device.pluginProps.get("states_list", indigo.List())
            new_states = indigo.List()                
            states_list = []
            for key in states_dict:
                new_states.append(key)
                states_list.append({'key': key, 'value': states_dict[key], 'decimalPlaces': 2})
            if old_states != new_states:
                self.logger.threaddebug(u"{}: update, new states_list: {}".format(device.name, new_states))
                newProps = device.pluginProps
                newProps["states_list"] = new_states
                device.replacePluginPropsOnServer(newProps)
                device.stateListOrDisplayStateIdChanged()    
            device.updateStatesOnServer(states_list)


        elif device.deviceTypeId == "shimGeneric":

            if bool(device.pluginProps.get('SupportsBatteryLevel', False)):
                battery = self.recurseDict(device.pluginProps['battery_payload_key'], state_data)
                device.updateStateOnServer('batteryLevel', battery, uiValue='{}%'.format(battery))

            if bool(device.pluginProps.get('SupportsEnergyMeter', False)) and ("accumEnergyTotal" in device.states):
                energy = self.recurseDict(device.pluginProps['energy_payload_key'], state_data)
                device.updateStateOnServer('accumEnergyTotal', energy, uiValue='{} kWh'.format(energy))

            if bool(device.pluginProps.get('SupportsEnergyMeterCurPower', False)) and  ("curEnergyLevel" in device.states):
                power = self.recurseDict(device.pluginProps['power_payload_key'], state_data)
                device.updateStateOnServer('curEnergyLevel', power, uiValue='{} W'.format(power))

            states_key = device.pluginProps.get('state_dict_payload_key', None)
            if not states_key:
                return
            try:
                data = json.loads(message_data["payload"])
            except:
                self.logger.debug(u"{}: JSON decode error for payload, aborting".format(device.name))
                return
            self.logger.threaddebug(u"{}: update state_dict_payload_key, key = {}".format(device.name, states_key))
            states_dict = self.recurseDict(states_key, data)
            if not states_dict:
                return
            elif type(states_dict) != dict:
                self.logger.error(u"{}: Device config error, bad Multi-States Key value: {}".format(device.name, states_key))
                return
            elif not len(states_dict) > 0:
                self.logger.warning(u"{}: Possible device config error, Multi-States Key {} returns empty dict.".format(device.name, states_key))
                return
             
            old_states =  device.pluginProps.get("states_list", indigo.List())
            new_states = indigo.List()                
            states_list = []
            for key in states_dict:
                new_states.append(key)
                states_list.append({'key': key, 'value': states_dict[key], 'decimalPlaces': 2})
            if old_states != new_states:
                self.logger.threaddebug(u"{}: update, new states_list: {}".format(device.name, new_states))
                newProps = device.pluginProps
                newProps["states_list"] = new_states
                device.replacePluginPropsOnServer(newProps)
                device.stateListOrDisplayStateIdChanged()    
            device.updateStatesOnServer(states_list)

        else:
            self.logger.warning(u"{}: Invalid device type: {}".format(device.name, device.deviceTypeId))

        # Now do any triggers

        for trigger in self.triggers.values():
            if trigger.pluginProps["shimDevice"] == str(device.id):
                if trigger.pluginTypeId == "deviceUpdated":
                    indigo.trigger.execute(trigger)                    
                elif trigger.pluginTypeId == "stateUpdated":
                    state_name = trigger.pluginProps["deviceState"]
                    if state_name in states_dict:
                        indigo.trigger.execute(trigger)                    
 
 
            
    def recurseDict(self, key_string, data_dict):
        self.logger.threaddebug(u"recurseDict key_string = {}, data_dict= {}".format(key_string, data_dict))
        try:
            if key_string == u'.':
                return data_dict
                
            elif '.' not in key_string:
                try:
                    if key_string[0] == '[':
                        new_data = data_dict[int(key_string[1:-1])]
                    else:
                        new_data = data_dict.get(key_string, None)
                except:
                    return None
                else:
                    return new_data
            
            else:
                split = key_string.split('.', 1)
                self.logger.threaddebug(u"recurseDict split[0] = {}, split[1] = {}".format(split[0], split[1]))
                try:
                    if split[0][0] == '[':
                        new_data = data_dict[int(split[0][1:-1])]
                    else:
                        new_data = data_dict[split[0]]
                except:
                    return None
                else:
                    return self.recurseDict(split[1], new_data)
        except Exception as e:
            self.logger.error(u"recurseDict error: {}".format(e))
              

    def getStateList(self, filter, valuesDict, typeId, deviceId):
        returnList = list()
        if 'states_list' in valuesDict:
            for topic in valuesDict['states_list']:
                returnList.append(topic)
        return returnList

    def getDeviceStateList(self, device):
        stateList = indigo.PluginBase.getDeviceStateList(self, device)
        add_states =  device.pluginProps.get("states_list", indigo.List())
        for key in add_states:
            dynamic_state = self.getDeviceStateDictForStringType(unicode(key), unicode(key), unicode(key))
            stateList.append(dynamic_state)
        self.logger.threaddebug(u"{}: getDeviceStateList returning: {}".format(device.name, stateList))
        return stateList 
    

    def getBrokerDevices(self, filter="", valuesDict=None, typeId="", targetId=0):

        retList = []
        devicePlugin = valuesDict.get("devicePlugin", None)
        for dev in indigo.devices.iter():
            if dev.protocol == indigo.kProtocol.Plugin and \
                dev.pluginId == "com.flyingdiver.indigoplugin.mqtt" and \
                dev.deviceTypeId != 'aggregator' :
                retList.append((dev.id, dev.name))

        retList.sort(key=lambda tup: tup[1])
        return retList


    ########################################
    # Relay / Dimmer Action callback
    ########################################

    def actionControlDevice(self, action, device):

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            action_template =  device.pluginProps.get("action_template", None)
            if not action_template:
                self.logger.error(u"{}: actionControlDevice: no action template".format(device.name))
                return
                
            payload =  device.pluginProps.get("on_action_payload", "on")
            topic = pystache.render(action_template, {'uniqueID': device.address})
            self.publish_topic(device, topic, payload)
            self.logger.info(u"Sent '{}' On".format(device.name))

        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            action_template =  device.pluginProps.get("action_template", None)
            if not action_template:
                self.logger.error(u"{}: actionControlDevice: no action template".format(device.name))
                return

            payload =  device.pluginProps.get("off_action_payload", "off")
            topic = pystache.render(action_template, {'uniqueID': device.address})
            self.publish_topic(device, topic, payload)
            self.logger.info(u"Sent '{}' Off".format(device.name))

        elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
            newBrightness = action.actionValue

            action_template =  device.pluginProps.get("dimmer_action_template", None)
            if not action_template:
                self.logger.error(u"{}: actionControlDevice: no action template".format(device.name))
                return
            payload_template =  device.pluginProps.get("dimmer_action_payload", None)
            if not payload_template:
                self.logger.error(u"{}: actionControlDevice: no payload template".format(device.name))
                return

            topic = pystache.render(action_template, {'uniqueID': device.address})
            payload = pystache.render(payload_template, {'brightness': newBrightness})
            self.publish_topic(device, topic, payload)
            self.logger.info(u"Sent '{}' Brightness = {}".format(device.name, newBrightness))

        else:
            self.logger.error(u"{}: actionControlDevice: Unsupported action requested: {}".format(device.name, action.deviceAction))

    ########################################
    # General Action callback
    ########################################

    def actionControlUniversal(self, action, device):

        action_template =  device.pluginProps.get("action_template", None)
        if not action_template:
            self.logger.error(u"{}: actionControlDevice: no action template".format(device.name))
            return
        topic = pystache.render(action_template, {'uniqueID': device.address})

        if action.deviceAction == indigo.kUniversalAction.RequestStatus or action.deviceAction == indigo.kUniversalAction.EnergyUpdate:
            self.logger.debug(u"{}: actionControlUniversal: RequestStatus".format(device.name))
            if not bool(device.pluginProps.get('SupportsStatusRequest', False)):
                self.logger.warning(u"{}: actionControlUniversal: device does not support status requests".format(device.name))                
            else:
                action_template =  device.pluginProps.get("status_action_template", None)
                if not action_template:
                    self.logger.error(u"{}: actionControlUniversal: no action template".format(device.name))
                    return
                payload =  device.pluginProps.get("status_action_payload", "")
                topic = pystache.render(action_template, {'uniqueID': device.address})
                self.publish_topic(device, topic, payload)
                self.logger.info(u"Sent '{}' Status Request".format(device.name))

#       elif action.deviceAction == indigo.kUniversalAction.EnergyReset:
#             self.logger.debug(u"{}: actionControlUniversal: EnergyReset".format(device.name))
#           dev.updateStateOnServer("accumEnergyTotal", 0.0)
 
        else:
            self.logger.error(u"{}: actionControlUniversal: Unsupported action requested: {}".format(device.name, action.deviceAction))


    def publish_topic(self, device, topic, payload):
    
        mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")
        if not mqttPlugin.isEnabled():
            self.logger.error(u"MQTT plugin not enabled, publish_topic aborting.")
            return

        brokerID = int(device.pluginProps['brokerID'])
        props = { 
            'topic': topic,
            'payload': payload,
            'qos': 0,
            'retain': 0,
        }
        mqttPlugin.executeAction("publish", deviceId=brokerID, props=props, waitUntilDone=False)                    
        self.logger.debug(u"{}: publish_topic: {} -> {}".format(device.name, topic, payload))

    ########################################
    # PluginConfig methods
    ########################################

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if not userCancelled:
            try:
                self.logLevel = int(valuesDict[u"logLevel"])
            except:
                self.logLevel = logging.INFO
            self.indigo_log_handler.setLevel(self.logLevel)

    ########################################
    # Custom Plugin Action callbacks (defined in Actions.xml)
    ######################
        
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
        for key in device.pluginProps:
            if isinstance(device.pluginProps[key], indigo.List):
                continue
            if isinstance(device.pluginProps[key], indigo.Dict):
                continue
            if key in ['brokerID', 'address']:
                continue
            props[key] = device.pluginProps[key]
        template['props'] = props

        for trigger in indigo.triggers:
            try:
                if trigger.pluginId == 'com.flyingdiver.indigoplugin.mqtt' and trigger.pluginTypeId == 'topicMatch':
                    if trigger.globalProps['com.flyingdiver.indigoplugin.mqtt']['message_type'] == props['message_type']:
                        match_list = []
                        for item in trigger.globalProps['com.flyingdiver.indigoplugin.mqtt']['match_list']:
                            match_list.append(item)
                        break
            except:
                pass
                    
        template['trigger'] = {
            'message_type': props['message_type'], 
            'match_list': json.dumps(match_list),
            'queueMessage': True
        }
                        
        self.logger.info("\n{}".format(yaml.safe_dump(template, allow_unicode=True, width=80, indent=4, default_flow_style=False).decode('utf-8')))

        return True

    def pickDeviceTemplate(self, filter=None, valuesDict=None, typeId=0, targetId=0):

        locations = ["./Templates", indigo.server.getInstallFolderPath() + '/' + self.pluginPrefs.get("externalTemplates", "MQTT Shim Templates")]
        templates = {}
        
        for template_dir in locations:

            # iterate through the  template directory, make list of names and paths
            # r=root, d=directories, f = files
        
            for root, d, f in os.walk(template_dir):
                for file in f:
                    self.logger.debug("Found File {} in {}".format(file, root))
                    (base, ext) = os.path.splitext(file)
                    if ext == '.yaml':
                        templates[base] = os.path.join(root, file)
                for dir in d:
                    self.logger.debug("Found directory {} in {}".format(dir, root))
                    # need to look in here too 
                    
        retList = []
        for key in templates:
            retList.append((templates[key], key))
        retList.sort(key=lambda tup: tup[1])
        self.logger.debug("{}".format(retList))
        return retList

    
    def createDeviceFromTemplate(self, valuesDict, typeId):
        self.logger.debug("createDeviceFromTemplate, typeId = {}, valuesDict = {}".format(typeId, valuesDict))
        stream = file(valuesDict['deviceTemplatePath'], 'r')
        template = yaml.safe_load(stream)
        template['props']['brokerID'] = valuesDict['brokerID']        
        try:
            indigo.device.create(indigo.kProtocol.Plugin, 
                name="{} {}".format(template['type'], valuesDict['address']), 
                address=valuesDict['address'], 
                deviceTypeId=template['type'], 
                props=template['props'])
        except Exception, e:
            self.logger.error("Error calling indigo.device.create(): {}".format(e.message))

        # create a trigger for this device if needed
        
        if bool(valuesDict['createTrigger']):
            try:
                indigo.pluginEvent.create(
                    name="{} {} Trigger".format(template['type'], valuesDict['address']), 
                    pluginId="com.flyingdiver.indigoplugin.mqtt",
                    pluginTypeId="topicMatch",
                    props={
                        "brokerID":     valuesDict['brokerID'],  
                        "message_type": template['trigger']['message_type'], 
                        "queueMessage": template['trigger']['queueMessage'], 
                        "match_list":   json.loads(template['trigger']['match_list']) 
                    })
            except Exception, e:
                self.logger.error("Error calling indigo.pluginEvent.create(): {}".format(e.message))
    
        return True
        
