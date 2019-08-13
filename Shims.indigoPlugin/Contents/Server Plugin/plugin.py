#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################

import logging
import json
import pystache

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
        indigo.server.log(u"Starting Shims")
        self.shimDevices = []
        indigo.server.subscribeToBroadcast(u"com.flyingdiver.indigoplugin.mqtt", u"com.flyingdiver.indigoplugin.mqtt-message_queued", "message_handler")

    def shutdown(self):
        indigo.server.log(u"Shutting down Shims")


    def deviceStartComm(self, device):
        self.logger.info(u"{}: Starting Device".format(device.name))

        instanceVers = int(device.pluginProps.get('devVersCount', 0))
        if instanceVers >= kCurDevVersCount:
            self.logger.threaddebug(u"{}: Device Version is up to date".format(device.name))
        elif instanceVers < kCurDevVersCount:
            newProps = device.pluginProps

            newProps["devVersCount"] = kCurDevVersCount
            device.replacePluginPropsOnServer(newProps)
            device.stateListOrDisplayStateIdChanged()
            self.logger.threaddebug(u"{}: Updated to version {}".format(device.name, kCurDevVersCount))
        else:
            self.logger.error(u"{}: Unknown device version: {}".format(device.name. instanceVers))

        assert device.id not in self.shimDevices
        self.shimDevices.append(device.id)


    def deviceStopComm(self, device):
        self.logger.info(u"{}: Stopping Device".format(device.name))
        assert device.id in self.shimDevices
        self.shimDevices.remove(device.id)

    def didDeviceCommPropertyChange(self, oldDevice, newDevice):
        return False


    def message_handler(self, notification):
        self.logger.debug(u"received notification of MQTT message type {} from {}".format(notification["message_type"], indigo.devices[int(notification["brokerID"])].name))

        mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")
        if not mqttPlugin.isEnabled():
            self.logger.error(u"MQTT plugin not enabled, message_handler aborting.")
            return
        
        message_data = None
        for deviceID in self.shimDevices:
            device = indigo.devices[deviceID]
            message_type = device.pluginProps['message_type']
            if notification["message_type"] == message_type:
                if not message_data:
                    props = { 'message_type': message_type }
                    brokerID =  int(device.pluginProps['brokerID'])
                    message_data = mqttPlugin.executeAction("fetchQueuedMessage", deviceId=brokerID, props=props, waitUntilDone=True)
                if message_data != None:
                    self.logger.debug(u"{}: message_handler: '{}' {} -> {}".format(device.name, message_type, '/'.join(message_data["topic_parts"]), message_data["payload"]))
                    self.update(device, message_data)
                    
    
    def update(self, device, message_data):    
        try:
            if device.pluginProps['uid_location'] == "topic":
                message_address = message_data["topic_parts"][int(device.pluginProps['uid_location_topic_field'])]
                self.logger.threaddebug(u"{}: update topic message_address = {}".format(device.name, message_address))
            elif device.pluginProps['uid_location'] == "payload":
                payload = json.loads(message_data["payload"])
                message_address = payload[device.pluginProps['uid_location_payload_key']]
                self.logger.debug(u"{}: update json message_address = {}".format(device.name, message_address))
            else:
                self.logger.debug(u"{}: update can't determine address location".format(device.name))
                return
        except Exception as e:
            self.logger.error(u"{}: update error determining device address field: {}".format(device.name, e))
            return
            
        if device.pluginProps['address'] != message_address:
            self.logger.debug(u"{}: update address mismatch: {} != {}".format(device.name, device.pluginProps['address'], message_address))
            return
            
        try:
            if device.pluginProps['state_location'] == "topic":
                i = int(device.pluginProps['state_location_topic'])
                self.logger.threaddebug(u"{}: update state_location_topic = {}".format(device.name, i))
                message_value = message_data["topic_parts"][i]
            elif (device.pluginProps['state_location'] == "payload") and (device.pluginProps['state_location_payload_type'] == "json"):
                key = device.pluginProps['state_location_payload_key']
                data = json.loads(message_data["payload"])
                self.logger.threaddebug(u"{}: update state_location_payload, key = {}".format(device.name, key))
                message_value = self.recurseDict(key, data)
            elif (device.pluginProps['state_location'] == "payload") and (device.pluginProps['state_location_payload_type'] == "raw"):
                message_value = message_data["payload"]
            else:
                self.logger.debug(u"{}: update can't determine value location".format(device.name))
            
        except Exception as e:
            self.logger.error(u"{}: update error determining message value: {}".format(device.name, e))
            return
        
        if message_value == None:
            self.logger.debug(u"{}: key {} not found in payload".format(device.name, key))
            return
        
        if device.deviceTypeId in ["shimRelay", "shimOnOffSensor"]:
            if message_value in ['Off', 'OFF', False, '0', 0]:
                value = False
            else:
                value = True

            self.logger.debug(u"{}: Updating state to {}".format(device.name, value))

            if device.pluginProps["shimSensorSubtype"] == "Generic":
                device.updateStateOnServer(key='onOffState', value=value)
                device.updateStateImageOnServer(indigo.kStateImageSel.None)

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
                    
                
        elif device.deviceTypeId == "shimValueSensor":
            try:
                value = float(message_value)
            except (TypeError, ValueError) as e:
                self.logger.error(u"{}: update unable to convert '{}' to float: {}".format(device.name, message_value, e))
                return
                           
            function = device.pluginProps.get("adjustmentFunction", None)            
            self.logger.threaddebug(u"{}: update adjustmentFunction: '{}'".format(device.name, function))
            if function:
                if 'indigo' in function:
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
                device.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensor)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} °F'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Temperature-C":
                precision = device.pluginProps.get("shimSensorPrecision", "1")
                device.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensor)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} °C'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Humidity":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.HumiditySensor)
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

            elif device.pluginProps["shimSensorSubtype"] == "Luminence":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.LightSensor)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} lux'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "Luminence%":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.LightSensor)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f}%'.format(value, prec=precision))

            elif device.pluginProps["shimSensorSubtype"] == "ppm":
                precision = device.pluginProps.get("shimSensorPrecision", "0")
                device.updateStateImageOnServer(indigo.kStateImageSel.None)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=int(precision), uiValue=u'{:.{prec}f} ppm'.format(value, prec=precision))


            else:
                self.logger.debug(u"{}: update, unknown shimSensorSubtype: {}".format(device.pluginProps["shimSensorSubtype"]))
                
            states_key = device.pluginProps.get('state_dict_payload_key', None)
            if not states_key:
                return
            data = json.loads(message_data["payload"])
            self.logger.threaddebug(u"{}: update state_dict_payload_key, key = {}".format(device.name, states_key))
            states_dict = self.recurseDict(states_key, data)
            if not (states_dict and len(states_dict) > 0):
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
            
    def recurseDict(self, key_string, data_dict):
        self.logger.threaddebug(u"recurseDict key_string = {}, data_dict= {}".format(key_string, data_dict))
        try:
            if '.' not in key_string:
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
        if device.deviceTypeId != "shimValueSensor":
            return stateList
        add_states =  device.pluginProps.get("states_list", indigo.List())
        for key in add_states:
            stateList.append({  "Disabled"     : False, 
                                "Key"          : key, 
                                "StateLabel"   : key,   
                                "TriggerLabel" : key,   
                                "Type"         : 150 })
        self.logger.threaddebug(u"{}: getDeviceStateList returning: {}".format(device.name, stateList))
        return stateList 
    

    def getBrokerDevices(self, filter="", valuesDict=None, typeId="", targetId=0):

        retList = []
        devicePlugin = valuesDict.get("devicePlugin", None)
        for dev in indigo.devices.iter():
            if dev.protocol == indigo.kProtocol.Plugin and dev.pluginId == "com.flyingdiver.indigoplugin.mqtt":
                retList.append((dev.id, dev.name))

        retList.sort(key=lambda tup: tup[1])
        return retList


    ########################################
    # Relay / Dimmer Action callback
    ########################################

    def actionControlDevice(self, action, device):

        action_template =  device.pluginProps.get("action_template", None)
        if not action_template:
            self.logger.error(u"{}: actionControlDevice: no action template".format(device.name))
            return
        topic = pystache.render(action_template, {'uniqueID': device.address})

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            self.logger.debug(u"{}: actionControlDevice: Turn On".format(device.name))
            self.publish_topic(device, topic, "On")

        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            self.logger.debug(u"{}: actionControlDevice: Turn Off".format(device.name))
            self.publish_topic(device, topic, "Off")

        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            self.logger.debug(u"{}: actionControlDevice: Toggle".format(device.name))
            self.publish_topic(device, topic, "Toggle")

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

        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            self.logger.debug(u"{}: actionControlUniversal: RequestStatus".format(device.name))
            self.publish_topic(device, topic, "")

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
