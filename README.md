# TeslaBuddy

Connect a [TeslaMate](https://github.com/adriankumpf/teslamate) instance to [Home Assistant](https://www.home-assistant.io/), using MQTT. It allows basic control of your Tesla vehicle via Home Assistant (currently, just starting and stopping charging, and changing the charge limit). GPS/location information is also shown in Home Assistant.

All devices are auto-discovered via Home Assistant using MQTT Auto-Discovery - no manual configuration is required.

This is designed to be run in Docker (like TeslaMate), but can be run standalone if desired using command line arguments.

## Configuraiton

Configuration can be given via the OS environment (or via the command line).

The full list of options can always be viewed by running the script with `-h`. To set an option via the OS environment, convert the command line long option to uppercase, and replace "-" with "\_", for example, the "--database-host" option would be:

```
DATABASE_HOST=postgres.local
```

### Example docker-compose.yml

This example assumes you have built _TeslaBuddy_ as follows:

```
docker build -t teslabuddy .
```

This is a snippet from a `docker-compose.yml` file, this would typically be along side the TeslaMate configuration, and assuming you are running Home Assistant in the same file. Note that the `DATABASE_*` values can match identically the same values used for TeslaMate (you can also create a dedicated user in Postgres with read-only access).

```
  teslabuddy:
    image: teslabuddy
    depends_on:
      - homeassistant
      - teslamate
      - postgres
      - mqtt
    restart: always
    environment:
      - DATABASE_USER=teslamate
      - DATABASE_PASS=securepassword
      - DATABASE_NAME=teslamate
      - DATABASE_HOST=postgres
      - MQTT_HOST=mqtt
      # - DEBUG=true
    volumes:
      - "/etc/localtime:/etc/localtime:ro"
```

If you are not running TeslaMate as part of the same docker-compose or swarm or have a different name for it, you will also need to include the `TESLAMATE_URL` to match your configuration, eg:

```
      - TESLAMATE_URL=https://teslamate.my.domain/
```

If your TeslaMate configuration also has several vehicles associated with it, you will also need to include the VIN of the desired vehicle, eg:

```
      - VIN=5Y123456789123456
```

### Home Assistant "Device Tracker"

An important component of a Home Assistant Device Tracker (I have figured out from trial and error as the docs don't cover this), is the `state` component should always be either `home` or `not_home`. To configure the `home` location, in TeslaMate create a Geo-Fence (configured via the web interface), and name it "Home". When the vehicle enters this area, it will set the state attribute to `home`. If not set, the vehicle will _always_ be not home. Home Assistant does **not** use it's configured home location to set this for device trackers via MQTT (I'm not sure about other devices).

# ToDo

Currently this only supports charging actions, and is very much focused on that. However if you are interested in supporting more actions, please raise an issue and I can include it. I will likely add further controls about what options are exposed via MQTT if going down this path.

Additionally, not all items from TeslaMate are surfaced in Home Assistant, please raise an issue if you want more to come through as well.

Finally, the configuration is fairly limited, so if something is not supported that you need (eg: MQTT over TLS), please also raise an issue.

If this becomes at all popular, I'll also look to deploy it to Docker Hub (so you don't have to build it yourself).

# Implementation Notes

This implementation uses the vehicle VIN as as identifier for everything to make sure it remains unique. This means that should a vehicle be move to/from another account, things should remain the same in Home Assistant, even if TeslaMate needs updating.

I use this to manage when my car will charge, to make the most of home solar generation and cheap grid prices (adjusting how much the vehicle will charge based on the current value).
