# Example decoder that expands nested dictionaries into a flat dictionary
#
# The class name must be the same as the file name, and the class must have a static method called "decode"
# that takes a single argument, which is the payload of the MQTT message.  The method must return a dictionary
# of states to update, or None if there are no states to update.

class TestDecoder(object):
    def __init__(self, name):
        self.name = name
        self.counter = 0

    def decode(self, payload):
    
        new_states = {'counter': self.counter}
        self.counter += 1

        for key in payload:
            if type(payload[key]) is dict:
                for subkey in payload[key]:
                    new_key = key + "_" + subkey
                    new_states[new_key] = payload[key][subkey]
        if len(new_states):
            return new_states
        else:
            return None
