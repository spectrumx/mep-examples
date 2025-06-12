#!/opt/radiohound/python313/bin/python
'''
Start the fft service which listens to /data/ringbuffer and generates an fft from the most recent 1024 samples.  
Author: Randy Herban
'''
import paho.mqtt.client as mqtt
import queue
import time
import json

payload = {
  'task_name': 'enable'
}

timeout = 10
response = queue.Queue() # This holds the messages from the node

# Create callback function for MQTT to receive message and add to queue
def on_message(client, userdata, msg):
  global response
  payload = json.loads(msg.payload.decode())
  # If we get a retained message (old data), ignore it
  if msg.retain==1:
    return
  response.put(payload)

client = mqtt.Client()
client.on_message = on_message
client.connect("localhost", 1883, 60)
client.subscribe("fft/status")
client.loop_start()

print(f"Sending command: {payload}")
client.publish("fft/command", payload=json.dumps(payload))

start_time = time.time()
# Enter a loop which exits after timeout or we receive data from the node
while True:  
    if time.time() - start_time > timeout:
      print(f"Timed out")
      break
     
    if not response.empty():
      message = response.get()
      print(message)
      break