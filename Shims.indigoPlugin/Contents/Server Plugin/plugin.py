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
        self.logger.debug(u"logLevel = " + str(self.logLevel))


    def startup(self):
        indigo.server.log(u"Starting Shims")
        self.shimDevices = {}
        indigo.server.subscribeToBroadcast(u"com.flyingdiver.indigoplugin.mqtt", u"com.flyingdiver.indigoplugin.mqtt-message_queued", "message_handler")

    def shutdown(self):
        indigo.server.log(u"Shutting down Shims")


    def message_handler(self, notification):
        self.logger.debug(u"received notification of MQTT message type {} from device {}".format(notification["message_type"], notification["brokerID"]))

        mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")
        if not mqttPlugin.isEnabled():
            self.logger.error(u"MQTT plugin not enabled, message_handler aborting.")
            return
        
        message_data = None
        for device in self.shimDevices.values():
            message_type = device.pluginProps['message_type']
            if notification["message_type"] == message_type:
                if not message_data:
                    props = { 'message_type': message_type }
                    brokerID =  int(device.pluginProps['brokerID'])
                    message_data = mqttPlugin.executeAction("fetchQueuedMessage", deviceId=brokerID, props=props, waitUntilDone=True)
                if message_data != None:
                    self.logger.debug(u"{}: sending {} to matchAndUpdate: {}".format(device.name, message_type, message_data["payload"]))
                    self.matchAndUpdate(device, message_data)
                    

    def deviceStartComm(self, device):
        self.logger.info(u"{}: Starting Device".format(device.name))

        instanceVers = int(device.pluginProps.get('devVersCount', 0))
        if instanceVers >= kCurDevVersCount:
            self.logger.debug(u"{}: Device Version is up to date".format(device.name))
        elif instanceVers < kCurDevVersCount:
            newProps = device.pluginProps

            newProps["devVersCount"] = kCurDevVersCount
            device.replacePluginPropsOnServer(newProps)
            device.stateListOrDisplayStateIdChanged()
            self.logger.debug(u"{}: Updated to version {}".format(device.name, kCurDevVersCount))
        else:
            self.logger.error(u"{}: Unknown device version: {}".format(device.name. instanceVers))

        assert device.id not in self.shimDevices
        self.shimDevices[device.id] = device


    def deviceStopComm(self, device):
        self.logger.info(u"{}: Stopping Device".format(device.name))
        assert device.id in self.shimDevices
        del self.shimDevices[device.id]
     
    def recurseDict(self, key_string, data_dict):
        self.logger.threaddebug(u"recurseDict key_string = {}, data_dict= {}".format(key_string, data_dict))
        try:
            if '.' not in key_string:
                if key_string[0] == '[':
                    return data_dict[int(key_string[1:-1])]
                else:
                    return data_dict[key_string]
            else:
                split = key_string.split('.', 1)
                self.logger.threaddebug(u"recurseDict split[0] = {}, split[1] = {}".format(split[0], split[1]))
                if split[0][0] == '[':
                    return self.recurseDict(split[1], data_dict[int(split[0][1:-1])])
                else:
                    return self.recurseDict(split[1], data_dict[split[0]])
        except Exception as e:
            self.logger.error(u"recurseDict error: {}".format(e))
              
    
    def matchAndUpdate(self, device, message_data):    
        try:
            if device.pluginProps['uid_location'] == "topic":
                message_address = message_data["topic_parts"][int(device.pluginProps['uid_location_topic_field'])]
                self.logger.debug(u"{}: matchAndUpdate topic message_address = {}".format(device.name, message_address))
            elif device.pluginProps['uid_location'] == "payload":
                payload = json.loads(message_data["payload"])
                message_address = payload[device.pluginProps['uid_location_payload_key']]
                self.logger.debug(u"{}: matchAndUpdate json message_address = {}".format(device.name, message_address))
            else:
                self.logger.debug(u"{}: matchAndUpdate can't determine address location".format(device.name))
                return
        except Exception as e:
            self.logger.error(u"{}: matchAndUpdate error determining device address field: {}".format(device.name, e))
            return
            
        if device.pluginProps['address'] != message_address:
            self.logger.debug(u"{}: matchAndUpdate address mismatch: {} != {}".format(device.name, device.pluginProps['address'], message_address))
            return
            
        try:
            if device.pluginProps['state_location'] == "topic":
                i = int(device.pluginProps['state_location_topic'])
                self.logger.threaddebug(u"{}: matchAndUpdate state_location_topic = {}".format(device.name, i))
                message_value = message_data["topic_parts"][i]
            elif (device.pluginProps['state_location'] == "payload") and (device.pluginProps['state_location_payload_type'] == "json"):
                key = device.pluginProps['state_location_payload_key']
                data = json.loads(message_data["payload"])
                self.logger.threaddebug(u"{}: matchAndUpdate state_location_payload, key = {}".format(device.name, key))
                message_value = self.recurseDict(key, data)
            elif (device.pluginProps['state_location'] == "payload") and (device.pluginProps['state_location_payload_type'] == "raw"):
                message_value = message_data["payload"]
            else:
                self.logger.debug(u"{}: matchAndUpdate can't determine value location".format(device.name))
            
        except Exception as e:
            self.logger.error(u"{}: matchAndUpdate error determining message value: {}".format(device.name, e))
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
                self.logger.error(u"{}: matchAndUpdate unable to convert '{}' to float: {}".format(device.name, message_value, e))
                return
                           
            function = device.pluginProps.get("conversionFunction", None)
            if function:
                x = value
                value = eval(function)

            self.logger.debug(u"{}: Updating state to {}".format(device.name, value))
    
            if device.pluginProps["shimSensorSubtype"] == "Generic":
                device.updateStateImageOnServer(indigo.kStateImageSel.None)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=2, uiValue=u'{:.2f}'.format(value))

            elif device.pluginProps["shimSensorSubtype"] == "Temperature-F":
                device.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensor)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=1, uiValue=u'{:.1f} °F'.format(value))

            elif device.pluginProps["shimSensorSubtype"] == "Temperature-C":
                device.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensor)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=1, uiValue=u'{:.1f} °C'.format(value))

            elif device.pluginProps["shimSensorSubtype"] == "Humidity":
                device.updateStateImageOnServer(indigo.kStateImageSel.HumiditySensor)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=0, uiValue=u'{:.0f}%'.format(value))

            elif device.pluginProps["shimSensorSubtype"] == "Pressure-inHg":
                device.updateStateImageOnServer(indigo.kStateImageSel.None)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=2, uiValue=u'{:.2f} inHg'.format(value))

            elif device.pluginProps["shimSensorSubtype"] == "Pressure-mb":
                device.updateStateImageOnServer(indigo.kStateImageSel.None)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=2, uiValue=u'{:.2f} mb'.format(value))

            elif device.pluginProps["shimSensorSubtype"] == "Power-W":
                device.updateStateImageOnServer(indigo.kStateImageSel.EnergyMeterOn)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=2, uiValue=u'{:.2f} W'.format(value))

            elif device.pluginProps["shimSensorSubtype"] == "Luminence":
                device.updateStateImageOnServer(indigo.kStateImageSel.LightSensor)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=0, uiValue=u'{:.0f} lux'.format(value))

            elif device.pluginProps["shimSensorSubtype"] == "Luminence%":
                device.updateStateImageOnServer(indigo.kStateImageSel.LightSensor)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=0, uiValue=u'{:.0f}%'.format(value))

            elif device.pluginProps["shimSensorSubtype"] == "ppm":
                device.updateStateImageOnServer(indigo.kStateImageSel.None)
                device.updateStateOnServer(key='sensorValue', value=value, decimalPlaces=0, uiValue=u'{:.0f} ppm'.format(value))


            else:
                self.logger.debug(u"{}: matchAndUpdate, unknown shimSensorSubtype: {}".format(device.pluginProps["shimSensorSubtype"]))
                
            states_key = device.pluginProps.get('state_dict_payload_key', None)
            if not states_key:
                return
            data = json.loads(message_data["payload"])
            self.logger.threaddebug(u"{}: matchAndUpdate state_dict_payload_key, key = {}".format(device.name, states_key))
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
                self.logger.threaddebug(u"{}: matchAndUpdate, new states_list: {}".format(device.name, new_states))
                newProps = device.pluginProps
                newProps["states_list"] = new_states
                device.replacePluginPropsOnServer(newProps)
                device.stateListOrDisplayStateIdChanged()    
            device.updateStatesOnServer(states_list)

        else:
            self.logger.warning(u"{}: Invalid device type: {}".format(device.name, device.deviceTypeId))

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
    # Device Control methods
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
