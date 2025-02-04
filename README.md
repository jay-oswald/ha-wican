[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

# About
This is the official HomeAssistant integration for [WiCAN by meatpi](https://github.com/meatpiHQ/wican-fw).

This integration is available via HACS, and not part of the default HomeAssistant integrations. 

# WiCAN Documentation
The setup of WiCAN devices (e.g. WiCAN OBD or WiCAN USB) can be found in the offical [WiCAN Documentation](https://meatpihq.github.io/wican-fw/).

There you will also find configuration instructions for the device itself (e.g. Firmware Updates / Retrieving data for your specific car model) 

# Integration Status
It is very much in an Alpha stage at the moment, and under constant changes, hoping to get it in a Beta state soon where we could recommend starting to use it.

# Installation
(see also: [WiCAN Documentation - Integration Setup](https://meatpihq.github.io/wican-fw/home-assistant/integration_setup) )

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

If you don't see data right away make sure to switch on ignition in your car to get access for WiCAN to the CAN-bus data. 
