# Example decoder that expands nested dictionaries into a flat dictionary.
#
# The class name must be the same as the file name, and the class must have a static method called "decode"
# that takes a single argument, which is the payload of the MQTT message.  The method must return a dictionary
# of states to update, or None if there are no states to update.
#
# Do not modify this file.  Copy it to a new file and modify the copy. This file will be overwritten when the plugin is updated.

class Expand(object):
    def __init__(self, name):
        self.name = name

    def recurse(self, payload, new_states, prefix):
        for key in payload:
            if type(payload[key]) is dict:
                self.recurse(payload[key], new_states, f"{prefix}_{key}")
            else:
                new_states[f"{prefix}_{key}"] = payload[key]

    def decode(self, payload):

        if type(payload) is not dict:
            return payload

        new_states = {}
        for key in payload:
            if type(payload[key]) is dict:
                self.recurse(payload[key], new_states, key)

        if len(new_states):
            return new_states
        else:
            return None
