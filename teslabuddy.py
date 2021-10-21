#!/usr/bin/env python3
"""
Connect a TeslaMate instance to Home Assistant, using MQTT

TeslaMate: https://github.com/adriankumpf/teslamate
Home Assistant: https://www.home-assistant.io/

Configuration can be given via the OS environment (or via the command line), by
converting the command line long option to uppercase, and replacing "-" with "_", 
for example, the "--database-host" option would be:

DATABASE_HOST=postgres.local

Like TeslaMate, this is designed to be run in Docker, but can be run standalone
if required using command line options.
"""
import os
import sys
import argparse
import queue
import time
import json
import math
import logging
import threading
import postgres

import paho.mqtt.client
import requests

# Number of retries to the Tesla API
COMMAND_RETRIES = 3
# Number of seconds between each retry attempt, scaled by 1.5 between each attempt
COMMAND_RETRY_DELAY = 10

# Number of seconds to cache the token from the TeslaMate DB
TOKEN_CACHE_TIME = 30

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s: %(levelname)s:%(name)s: %(message)s"
)
log = logging.getLogger(__name__)

GPS_TOPICS = {"elevation", "longitude", "geofence", "latitude", "speed", "heading"}
MAP_THROUGH_TOPICS = {
    "battery_level",
    "charge_limit_soc",
    "charger_actual_current",
    "charger_power",
    "charger_voltage",
    "inside_temp",
    "odometer",
    "outside_temp",
    "state",
    "time_to_full_charge",
}


class TeslaBuddy:
    def __init__(self) -> None:
        self.config = self._initconfig()
        self.gpsq = queue.Queue()
        self.teslapiq = queue.Queue()
        self.teslamateq = queue.Queue()
        # Store target command state in a dict so several commands will replace each
        # other rather than an internal queue where all updates would eventually hit
        # the Tesla API, potentially resulting in ratelimits getting hit sooner.
        self._pubstate = {}

        self._tokencache = {}

        self.tmid: int = -1
        self.eid: int = -1
        self.carname: str | None = None
        self.carmodeltxt: str | None = None
        self.error_sleep_time = COMMAND_RETRY_DELAY
        self.teslamatesettings = None

        self.basetopic = ""

        self.teslamatesetup()

    def teslamatesetup(self):
        """Figure out the TeslaMate car ID and name from the VIN (if set)

        If VIN is not set, defaults to 1
        If not found, an error is raised.
        """
        if self.config.vin is None:
            whereclause = "settings_id = 1"
            whereargs = []
        else:
            whereclause = "vin = %s"
            whereargs = [self.config.vin]

        cardata = self.getdbconn().one(
            f"SELECT settings_id, vin, eid, name, trim_badging, model FROM cars WHERE {whereclause}",
            whereargs,
        )
        if cardata is None:
            raise ValueError(
                f"The VIN {self.config.vin} or car id 1 was not found in the TeslaMate database"
            )
        self.tmid = cardata[0]
        self.vin = cardata[1]
        self.eid = cardata[2]
        self.carname = cardata[3]
        self.carmodeltxt = f"Model {cardata[5]} {cardata[4]}"

        self.teslamatesettings = self.getdbconn().one("SELECT * FROM settings LIMIT 1;")

        self.basetopic = self.config.base_topic
        while self.basetopic.endswith("/"):
            self.basetopic = self.basetopic[:-1]
        self.basetopic += "/" + self.vin

    def getdbconn(self) -> postgres.Postgres:
        "Return a connection to the TeslaMate DB"
        dburl = f"postgres://{self.config.database_user}:{self.config.database_pass}@{self.config.database_host}:{self.config.database_port}/{self.config.database_name}"
        conn = postgres.Postgres(dburl)
        return conn

    def start(self):
        self.client = paho.mqtt.client.Client()
        self.client.on_connect = self.onmqttconnect
        self.client.on_message = self.onmqttmessage
        self.client.connect(self.config.mqtt_host)
        self.client.loop_start()

        # Thread to moanage bundling GPS information into a single message
        threading.Thread(target=self.gpsbundlethread, daemon=True).start()
        # Thread to manage waking TeslaMate when incoming commands happen
        threading.Thread(target=self.waketeslamatethread, daemon=True).start()

        self.homeassistantsetup()
        # Run the tesla thread, resume on error
        while 1:
            try:
                self.teslacomandthread()
            except Exception as e:
                log.exception("Error in tesla thread: %s", e)
                # traceback.print_exc()
                log.info("Sleeping %0.1f seconds from error", self.error_sleep_time)
                time.sleep(self.error_sleep_time)
                self.error_sleep_time *= 1.5

    def onmqttconnect(self, client, userdata, flags, rc):
        self.client.subscribe(f"{self.basetopic}/+/set")
        self.client.subscribe(f"teslamate/cars/{self.tmid}/+")

    def onmqttmessage(self, client, userdata, msg):
        payload = msg.payload.decode()
        topic: str = msg.topic
        parts = topic.split("/")

        log.debug("Incomming MQTT Message: %s : %s", topic, payload)
        if topic.startswith("teslamate/cars/"):
            self.teslamatemsg(parts[3], payload)
        elif topic.startswith(self.basetopic):
            if parts[-1] == "set":
                self.teslapiq.put((parts[-2], payload))

    def teslamatemsg(self, topic, value):
        "Process as message from TeslaMate"
        if topic in GPS_TOPICS:
            self.gpsq.put((topic, value))

        elif topic in MAP_THROUGH_TOPICS:
            self.pubifchanged(topic, value)

        elif topic == "plugged_in":
            if value == "true":
                value = "ON"
            else:
                value = "OFF"
            self.pubifchanged(topic, value)

        elif topic == "shift_state":
            if not value:
                value = "P"
            self.pubifchanged(topic, value)

        else:
            print("TBC:", topic, value)

        if topic == "state":
            # Also update the charging/not charging switch
            if value == "charging":
                txt = "ON"
            else:
                txt = "OFF"
            self.pubifchanged("charging", txt)

    def gpsbundlethread(self):
        "Waits for a full 'batch' of GPS location values before sending to HASS"
        # Time to wait for any more messages to come in to populate the current_state
        # TeslaMate sends all updates (on different topics) at the same moment
        QUEUE_TIMEOUT = 0.1
        current_state = {
            "latitude": None,
            "heading": 0,
            "longitude": None,
            "geofence": "",
            "speed": 0,
            "elevation": 0,
            "state": "not_home",  # Used by HASS, matches "Home" TeslaMate geofence
            "gps_accuracy": 1,  # HASS requires this, always set to 1
        }

        timeout = None
        while 1:
            try:
                print("Waiting:", timeout)
                topic, value = self.gpsq.get(block=True, timeout=timeout)
                print("Got from queue", topic, value)
            except queue.Empty:
                # New data has come in, with no updates, broadcast to HASS
                print("Timedout!")
                timeout = None
                if (
                    current_state["latitude"] is None
                    or current_state["longitude"] is None
                ):
                    # Don't try to send anyting if the lat/long is not set
                    print("Not sending:", current_state)
                    continue
                self.pubifchanged("gps", json.dumps(current_state))
                continue

            if topic == "geofence":
                current_state["geofence"] = value
                if value.lower() == "home":
                    current_state["state"] = "home"
                else:
                    current_state["state"] = "not_home"

            if topic in ("elevation", "longitude", "latitude", "speed", "heading"):
                current_state[topic] = forcefloat(value)

            timeout = QUEUE_TIMEOUT

    def waketeslamate(self):
        "Wake TeslaMate right away to get latest information"
        self.teslamateq.put("wake")

    def waketeslamatethread(self):
        """Attempts to wake the TeslaMate thread when required

        This ignores errors, and will not retry.
        """
        while 1:
            try:
                self.teslamateq.get(block=True, timeout=None)
                while self.teslamateq.qsize() > 0:
                    # Empty the queue to prevent rapid multiple requests
                    self.teslamateq.get(block=False)
                if not self.config.teslamate_url.startswith("http"):
                    log.debug(
                        "Clearly invalid TeslaMate URL, ignoring: %r",
                        self.config.teslamate_url,
                    )
                    continue
                baseurl = self.config.teslamate_url
                while baseurl.endswith("/"):
                    baseurl = baseurl[:-1]
                url = f"{baseurl}/api/car/{self.tmid}/logging/resume"
                log.debug("Waking TeslaMate at URL: %s", url)
                requests.put(url)
                # Slow down repeated requests
                time.sleep(1)
            except Exception as e:
                log.debug("Error making call to TeslaMate: %s", e)

    def _initconfig(self):
        parser = argparse.ArgumentParser(
            description="Connect TeslaMate to Home Assistant via MQTT.\n"
            "Unknown arguments are ignored (including typos)",
        )
        parser.add_argument(
            "--database-host",
            help="host name of the Postgres server",
            required=True,
        )
        parser.add_argument(
            "--database-user",
            help="username to access the Postgres server - this can be a read only user",
            required=True,
        )
        parser.add_argument(
            "--database-pass",
            help="password for the --database-user",
            required=True,
        )
        parser.add_argument(
            "--database-name",
            help="name of the database in postgres to connect to",
            required=True,
        )
        parser.add_argument(
            "--database-port",
            help="port of the postgres server to connect to",
            default=5432,
            type=int,
        )

        parser.add_argument(
            "--mqtt-host",
            help="MQTT broker host name",
            required=True,
        )
        parser.add_argument(
            "--mqtt-port",
            help="MQTT broker port",
            default=1883,
            type=int,
        )

        parser.add_argument(
            "--teslamate-url",
            help="base URL for TeslaMate, default is http://teslamate:4000/",
            default="http://teslamate:4000/",
        )

        parser.add_argument(
            "--vin",
            help="the VIN of the desired vehicle, "
            "only required if more than one vehicle on the account",
        )

        parser.add_argument(
            "--base-topic",
            help="base MQTT topic for pub/sub messages, no trailing /",
            default="tesla/car",
        )

        parser.add_argument(
            "--debug",
            help='if set to "true", will include debug level logging',
        )

        # Get the OS environnement arguments
        cmdlineargs = sys.argv.copy()[1:]
        for arg, val in os.environ.items():
            arg = arg.lower()
            arg = arg.replace("_", "-")
            if val:
                arg = f"--{arg}={val}"
            else:
                arg = f"--{arg}"
            cmdlineargs.append(arg)

        args = parser.parse_known_args(cmdlineargs)[0]
        if args.debug:
            if args.debug.lower() == "true":
                args.debug = True
            logging.getLogger().setLevel(logging.DEBUG)
        if args.debug is not True:
            args.debug = False
        # log.debug("Processed command line arguments: %s", cmdlineargs)
        log.debug("Final arguments: %s", args)
        return args

    def gettoken(self):
        """Get the current Tesla token from TeslaMate

        This does some basic minor caching of the token
        """
        if time.time() > self._tokencache.get("expiry", 1):
            self._tokencache["token"] = self.getdbconn().one(
                "SELECT access FROM tokens LIMIT 1;"
            )
            self._tokencache["expiry"] = time.time() + TOKEN_CACHE_TIME
        return self._tokencache["token"]

    def teslacomandthread(self):
        """Send off any comand requests to the Tesla API, including retrys

        Keeps a target state so if several updates for the same value come through
        only the current value is set if having issues/delays
        """
        targetstate = {}
        timeout = None
        errortimeout = COMMAND_RETRY_DELAY
        errortries = 0
        while 1:
            setting = None
            try:
                setting = self.teslapiq.get(block=True, timeout=timeout)
            except queue.Empty:
                pass

            if setting:
                # Got a setting, store and read out anything else left in queue
                while 1:
                    targetstate[setting[0]] = setting[1]
                    try:
                        setting = self.teslapiq.get(block=False)
                    except queue.Empty:
                        break

            if not targetstate:
                timeout = None
                continue

            try:
                # XXX Check permissions
                key, value = list(targetstate.items())[0]
                if key == "charge_limit_soc":
                    val = forceint(value)
                    if val >= 50 and val <= 100:
                        # Valid, make the request
                        self.teslaapireq(
                            "set_charge_limit", {"percent": val}, ["already_set"]
                        )

                elif key == "charging":
                    if value == "ON":
                        self.teslaapireq(
                            "charge_start", okreasons=["charging", "complete"]
                        )
                    elif value == "OFF":
                        self.teslaapireq("charge_stop", okreasons=["not_charging"])

                # If we got here, no errors were raised, remove it from the state
                del targetstate[key]
                errortimeout = COMMAND_RETRY_DELAY
                errortries = 0
                self.waketeslamate()
            except Exception as e:
                log.info("Error making Tesla API call: %s", e)
                log.debug("Sleeping %s seconds", errortimeout)
                time.sleep(errortimeout)
                timeout = 1
                errortimeout *= 1.5
                errortries += 1

            if errortries >= COMMAND_RETRIES:
                raise Exception("Command error retries hit, existing loop")

    def teslaapireq(self, command, payload={}, okreasons=[]):
        """Make a request to the Tesla API, no errors if all OK"""
        r = requests.post(
            f"https://owner-api.teslamotors.com/api/1/vehicles/{self.eid}/command/{command}",
            json=payload,
            headers={"Authorization": f"Bearer {self.gettoken()}"},
        )
        print(
            "Response:",
            r.content,
            f"Authorization: Bearer {self.gettoken()}",
        )
        response = r.json()["response"]
        if "error" in response:
            # {"response": None, "error": '{"error": "timeout"}', "error_description": ""}
            errtxt = (
                f"{response['error']} {response.get('error_description', '')}".strip()
            )
            raise requests.RequestException(f"Tesla API Error: {errtxt}")
        if response["result"] is True:
            return
        elif response["reason"] in okreasons:
            return

        raise requests.RequestException(f"Tesla API Error: {response['reason']}")

    def pubifchanged(self, item: str, value: str):
        """Publish to MQTT item (self.basetopic will be applied), with value.

        An item is only published if it has changed from when previously published.
        """
        if self._pubstate.get(item) != value:
            self.client.publish(f"{self.basetopic}/{item}", value)
            self._pubstate[item] = value

    def homeassistantsetup(self):
        "Publish config for Home Assistant"
        STANDARD_TOPICS = [
            # [topic, hass type, description, unit of measurement, class, icon]
            [
                "state",
                "sensor",
                "State",
                None,
                None,
                "mdi:gauge",
            ],
            [
                "shift_state",
                "sensor",
                "Shift State",
                None,
                None,
                None,
            ],
            [
                "outside_temp",
                "sensor",
                "Outside Temperature",
                "°" + self.teslamatesettings.unit_of_temperature,
                "temperature",
                None,
            ],
            [
                "inside_temp",
                "sensor",
                "Inside Temperature",
                "°" + self.teslamatesettings.unit_of_temperature,
                "temperature",
                None,
            ],
            [
                "time_to_full_charge",
                "sensor",
                "Time to Full",
                "h",
                None,
                "hass:clock-fast",
            ],
            [
                "odometer",
                "sensor",
                "Odometer",
                self.teslamatesettings.unit_of_length,
                None,
                "mdi:counter",
            ],
            [
                "charger_power",
                "sensor",
                "Charger Power",
                "kW",
                "power",
                None,
            ],
            [
                "charger_voltage",
                "sensor",
                "Charger Voltage",
                "V",
                "voltage",
                None,
            ],
            [
                "charger_actual_current",
                "sensor",
                "Charger Current",
                "A",
                "current",
                None,
            ],
            [
                "ideal_battery_range_km",
                "sensor",
                "Ideal Battery Range",
                "km",
                None,
                None,
            ],
            [
                "est_battery_range_km",
                "sensor",
                "Estimated Battery Range",
                "km",
                None,
                None,
            ],
            [
                "plugged_in",
                "binary_sensor",
                "Plugged In",
                None,
                None,
                None,
            ],
        ]

        # Special case to handle the device element
        self.client.publish(
            f"homeassistant/sensor/{self.vin}/battery/config",
            json.dumps(
                {
                    "name": f"{self.carname} Battery Level",
                    "state_topic": f"{self.basetopic}/battery_level",
                    "unique_id": f"{self.vin}_battery_level",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                    "device": {
                        "identifiers": [f"{self.vin}_device"],
                        "name": f"{self.carname} Vehicle",
                        "manufacturer": "Tesla",
                        "model": self.carmodeltxt,
                    },
                }
            ),
            retain=True,
        )

        for topic, hasstype, description, uom, devclass, icon in STANDARD_TOPICS:
            data = {
                "name": f"{self.carname} {description}",
                "state_topic": f"{self.basetopic}/{topic}",
                "unique_id": f"{self.vin}_{topic}",
                "device": {"identifiers": [f"{self.vin}_device"]},
            }
            if uom:
                data["unit_of_measurement"] = uom
            if devclass:
                data["device_class"] = devclass
            if icon:
                data["icon"] = icon

            self.client.publish(
                f"homeassistant/{hasstype}/{self.vin}/{topic}/config",
                json.dumps(data),
                retain=True,
            )

        # Charge limit, including setting
        self.client.publish(
            f"homeassistant/number/{self.vin}/charge_limit_soc/config",
            json.dumps(
                {
                    "name": f"{self.carname} Charge Limit",
                    "state_topic": f"{self.basetopic}/charge_limit_soc",
                    "command_topic": f"{self.basetopic}/charge_limit_soc/set",
                    "unique_id": f"{self.vin}_charge_limit_soc",
                    "min": 50,
                    "max": 100,
                    "device": {"identifiers": [f"{self.vin}_device"]},
                    "icon": "hass:battery-alert",
                }
            ),
            retain=True,
        )

        # Charging action, including setting/turning on/off
        self.client.publish(
            f"homeassistant/switch/{self.vin}/charging/config",
            json.dumps(
                {
                    "name": f"{self.carname} Charging",
                    "state_topic": f"{self.basetopic}/charging",
                    "command_topic": f"{self.basetopic}/charging/set",
                    "unique_id": f"{self.vin}_charging",
                    "device": {"identifiers": [f"{self.vin}_device"]},
                    "icon": "hass:battery-alert",  # XXX better icon?
                }
            ),
            retain=True,
        )

        self.client.publish(
            f"homeassistant/device_tracker/{self.vin}/gps/config",
            json.dumps(
                {
                    "name": f"{self.carname} Location",
                    "json_attributes_topic": f"{self.basetopic}/gps",
                    "state_topic": f"{self.basetopic}/gps",
                    "value_template": "{{value_json.state}}",
                    "unique_id": f"{self.vin}_gps",
                    "device": {"identifiers": [f"{self.vin}_device"]},
                    "source_type": "gps",
                    "icon": "mdi:crosshairs-gps",
                }
            ),
            retain=True,
        )


def forcefloat(v):
    try:
        return float(v)
    except:
        return 0


def forceint(v):
    return int(forcefloat(v))


def main():
    t = TeslaBuddy()
    t.start()


if __name__ == "__main__":
    main()
