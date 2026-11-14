"""The webhooks of removed entries: they answer 410 Gone.

Home Assistant answers a post to a webhook it does not know with 200, on
purpose, so a device that was asleep or away when its entry was removed went
on posting for good and counted every post as delivered. Removing an entry
asks the device to stop when it can be reached (``async_release_webhook_at``).
For the one that cannot, the webhook id of the removed entry is kept in
``.storage`` and stays registered with a handler that answers ``410 Gone``
and ``{"webhook": {"removed": true}}``, the way Home Assistant's own
``mobile_app`` tells its apps. Firmware 6.0.0-beta.11 and later clears its
webhook on that answer; older firmware counts its posts as failed from then
on, which is at least true.

The ids are registered when the first entry is set up and when a device is
discovered. With no entry left and no discovery, nothing loads this
integration after a restart of Home Assistant, and a post gets Home
Assistant's 200 again.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from http import HTTPStatus
import logging
from typing import TYPE_CHECKING, Any

from aiohttp.web import json_response
from homeassistant.components import webhook
from homeassistant.helpers.storage import Store
from homeassistant.util.json import json_loads

from .const import (
    DOMAIN,
    MAX_REMOVED_WEBHOOKS,
    REMOVED_WEBHOOKS_STORAGE_KEY,
    REMOVED_WEBHOOKS_STORAGE_VERSION,
    WEBHOOK_SETTINGS_MAX_BYTES,
)
from .helpers import async_read_capped

if TYPE_CHECKING:
    from collections.abc import Collection

    from aiohttp import ClientSession
    from aiohttp.web import Request, Response
    from homeassistant.core import HomeAssistant
    from yarl import URL

_LOGGER = logging.getLogger(__name__)

_HASS_DATA_KEY = f"{DOMAIN}_removed_webhooks"
_MAX_WEBHOOK_ID_LENGTH = 128

# What a device that reads the reply to its push acts on. A bare 410 could be
# any server's; this body is only ours.
REMOVED_WEBHOOK_REPLY = {"webhook": {"removed": True}}


def _usable_id(webhook_id: Any) -> bool:
    return (
        isinstance(webhook_id, str)
        and bool(webhook_id)
        and len(webhook_id) <= _MAX_WEBHOOK_ID_LENGTH
    )


class RemovedWebhooks:
    """The webhook ids of removed entries, each answering 410 Gone."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, REMOVED_WEBHOOKS_STORAGE_VERSION, REMOVED_WEBHOOKS_STORAGE_KEY,
        )
        self._lock = asyncio.Lock()
        self._loaded = False
        self._ids: list[str] = []
        # Ids whose device was told once in this run (one line in the log).
        self._told: set[str] = set()

    @property
    def ids(self) -> tuple[str, ...]:
        """Return the ids, oldest first."""
        return tuple(self._ids)

    async def async_load(self) -> None:
        """Read the stored ids and register each, once per run."""
        async with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                stored = await self._store.async_load()
            except Exception as err:  # a damaged file must not fail a setup
                _LOGGER.warning("Could not read the removed webhooks: %s", err)
                stored = None
            raw = stored.get("ids") if isinstance(stored, dict) else None
            ids = [item for item in raw if _usable_id(item)] if isinstance(raw, list) else []
            self._ids = list(dict.fromkeys(ids))[-MAX_REMOVED_WEBHOOKS:]
            for webhook_id in self._ids:
                self._register(webhook_id)

    async def async_add(self, webhook_id: str) -> None:
        """Keep the webhook of a removed entry answering 410 Gone."""
        await self.async_load()
        if not _usable_id(webhook_id) or webhook_id in self._ids:
            return
        self._ids.append(webhook_id)
        # The oldest go: a device that still posts to one after this many
        # removals gets Home Assistant's 200 again.
        while len(self._ids) > MAX_REMOVED_WEBHOOKS:
            webhook.async_unregister(self._hass, self._ids.pop(0))
        self._register(webhook_id)
        await self._async_save()

    async def async_forget(self, webhook_id: str) -> None:
        """Give a webhook id back to an entry that uses it again."""
        await self.async_load()
        if webhook_id not in self._ids:
            return
        self._ids.remove(webhook_id)
        webhook.async_unregister(self._hass, webhook_id)
        await self._async_save()

    def _register(self, webhook_id: str) -> None:
        # Taken by a loaded entry (a restored backup): the entry keeps it.
        with suppress(ValueError):
            webhook.async_register(
                self._hass, DOMAIN, "Removed MeatPi device", webhook_id, self._async_handle,
            )

    async def _async_save(self) -> None:
        try:
            await self._store.async_save({"ids": list(self._ids)})
        except Exception as err:  # the ids still answer 410 until a restart
            _LOGGER.warning("Could not store the removed webhooks: %s", err)

    async def _async_handle(
        self, hass: HomeAssistant, webhook_id: str, request: Request,  # noqa: ARG002
    ) -> Response:
        """Answer a post to the webhook of a removed entry."""
        if webhook_id not in self._told:
            self._told.add(webhook_id)
            _LOGGER.info(
                "A device at %s still posts to the webhook of a removed entry: "
                "answered 410 Gone, firmware that understands it stops posting",
                request.remote,
            )
        return json_response(REMOVED_WEBHOOK_REPLY, status=HTTPStatus.GONE)


async def async_get_removed_webhooks(hass: HomeAssistant) -> RemovedWebhooks:
    """Return the removed webhooks, loaded and registered."""
    removed: RemovedWebhooks | None = hass.data.get(_HASS_DATA_KEY)
    if removed is None:
        removed = hass.data[_HASS_DATA_KEY] = RemovedWebhooks(hass)
    await removed.async_load()
    return removed


def settings_name_webhook(settings: dict[str, Any], webhook_ids: Collection[str]) -> bool:
    """Return True when a device's webhook settings post to one of the webhooks."""
    urls: list[Any] = [settings.get("url")]
    more = settings.get("urls")
    if isinstance(more, list):
        urls.extend(more)
    paths = tuple(f"/api/webhook/{webhook_id}" for webhook_id in webhook_ids)
    return any(isinstance(url, str) and url.endswith(paths) for url in urls)


async def async_release_webhook_at(
    session: ClientSession, endpoint: URL, webhook_ids: Collection[str],
) -> bool | None:
    """Clear the device's webhook at one address when it is still one of ours.

    True: cleared. False: the device answered and keeps its settings.
    None: no usable answer at this address, another one may give it.
    """
    response = await session.get(str(endpoint))
    body = await async_read_capped(response, WEBHOOK_SETTINGS_MAX_BYTES)
    if response.status != HTTPStatus.OK or body is None:
        return None
    settings = json_loads(body)
    if not isinstance(settings, dict):
        return None
    if not settings_name_webhook(settings, webhook_ids):
        _LOGGER.debug(
            "The device at %s posts elsewhere by now; its webhook is left alone",
            endpoint,
        )
        return False
    if settings.get("manual_override") is True:
        _LOGGER.info(
            "The webhook is pinned on the device at %s (manual override): "
            "it keeps posting until it is switched off there",
            endpoint,
        )
        return False
    response = await session.delete(str(endpoint))
    await response.read()
    if response.status >= HTTPStatus.MULTIPLE_CHOICES:
        return None
    _LOGGER.info("Device webhook cleared at %s: the device stops posting", endpoint)
    return True
