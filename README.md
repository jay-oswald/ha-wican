[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

# About
This is the official HomeAssistant integration for [WiCAN by meatpi](https://github.com/meatpiHQ/wican-fw).

This integration is available via HACS, and not part of the default HomeAssistant integrations. 

# Documentation
This repository contains only documentation for the HomeAssistant integration of WiCAN.

The documentation for the devices (e.g. WiCAN OBD or WiCAN USB) can be found in the offical [WiCAN Device Documentation](https://meatpihq.github.io/wican-fw/).
There you will also find configuration instructions for the device itself (e.g. Firmware Updates / Retrieving data for your specific car model) 

# Integration Status
It is very much in an Alpha stage at the moment, and under constant changes, hoping to get it in a Beta state soon where we could recommend starting to use it.

# Installation

## Manual Installation
1. Add the integration repository to HACS and install the WiCAN integration.
   - Follow the official guide to [add a custom repository](https://www.hacs.xyz/docs/faq/custom_repositories/).
     - Repository URL: 'https://github.com/jay-oswald/ha-wican'
     - Type: 'Integration'
   - Follow the official guide to [download a repository](https://www.hacs.xyz/docs/use/repositories/dashboard/#downloading-a-repository)
2. Restart home assistant
3. Continue with Configuration steps below

## Installation via My Home Assistant
1. Add the integration through this link: 
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jay-oswald&repository=ha-wican&category=integration)
2. Restart home assistant
3. Continue with Configuration steps below

## Configuration
- In Home Assistant, go to 'Settings > Devices & Services > Integrations'.
- Click on 'Add Integration', search for WiCAN, and select it.
- Enter the mDNS/hostname (wican_xxxxxxxxxxxx.local) or IP-Address of WiCAN device to connect the WiCAN device. If you have multiple WiCAN devices repeat these steps for the other devices.

Result: After completing installation and configuration, WiCAN will be connected to Home Assistant, and you will be able to monitor the available car parameters directly from the Home Assistant interface.

# Troubleshooting
### Not possible to add a device via IP-Address or mDNS/hostname
Potential root cause: The WiCAN device might not be accessible or the protocol is not set to "AutoPID".

To fix the issue:
1. Please make sure that the WiCAN device is accesssible from your web browser. If it is not available, ensure that it is not in sleep mode [WiCAN Docs: Sleep Mode](https://meatpihq.github.io/wican-fw/config/sleep-mode)
2. Please make sure that the WiCAN device uses protocol "AutoPID" via the WiCAN device settings.

### The device is added, but all entites show status "Unavailable"
Potential root cause: HomeAssistant has been restarted or the WiCAN integration reloaded while the WiCAN device was not available (e.g. car away, sleep mode).

To fix the issue, make sure, the WiCAN device is available (e.g. by turning on ignition of car) and then reload the integration.

### Device entities are not properly updated anymore after changing the car configuration on the WiCAN device
Potential root cause: The WiCAN integration creates entities based on the car configuration in HomeAssistant. By changing the car configuration, some PIDs might get added and others removed.

To ensure, that all entities in HomeAssistant are up to date after changing the car configuration, you can either
* delete inidividual entities, that are not available in the new car configuration OR
* delete the WiCAN device in HomeAssistant and afterwards add it again with the new car configuration.

### The Unit of measure of a device entity cannot be changed in HomeAssistant
Background: The WiCAN HomeAssistant integration creates entities based on the WiCAN car configuration.

To change the unit of measure of an entity in HomeAssistant, it needs to be updated in the WiCAN device itself:
* Open the WiCAN device in a web-browser (e.g. via link "VISIT" from the WiCAN device page in HomeAssistant)
* Go to tab "Automate", find the respective PID, update the "unit" and press "Submit changes". Further details about the car configuration are part of the official WiCAN device documentation: [Automate](https://meatpihq.github.io/wican-fw/config/automate/usage)
* After changing the unit on the WiCAN device, go to HomeAssistant and reload the WiCAN integration. This will automatically update the unit of measure for the respective entity.
