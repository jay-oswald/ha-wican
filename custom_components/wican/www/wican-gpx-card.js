/* WiCAN GPS track card (2026-11-06)
 *
 * Shows the track a WiCAN recorded on its SD card on Home Assistant's own
 * map element. The track comes from the integration's `wican.save_gpx`
 * service (the device's GET /api/logger/gpx, saved under www/wican) or
 * from a GPX file already under /local/.
 *
 *   type: custom:wican-gpx-card
 *   device_id: <the WiCAN device>      the device whose track to show
 *   hours: 24                          how far back (1..720), with device_id
 *   url: /local/wican/<file>.gpx       a saved file instead of device_id
 *   title: GPS track                   the card's header (empty hides it)
 *   height: 320                        the map's height in px
 *   color: "#1e88e5"                   the track's colour
 *
 * The integration serves this file and registers it as a frontend
 * resource, so the card is available without a manual resource entry.
 */
(() => {
  const TAG = "wican-gpx-card";
  if (customElements.get(TAG)) {
    return;
  }

  const DEFAULT_HOURS = 24;
  const MAX_HOURS = 24 * 30;

  /* GPX 1.1 text -> the map element's paths (one per track segment) */
  function parseGpx(text, color) {
    const doc = new DOMParser().parseFromString(text, "application/xml");
    if (doc.querySelector("parsererror")) {
      throw new Error("The track file is not valid GPX");
    }
    const paths = [];
    let points = 0;
    let first = null;
    let last = null;
    const segments = doc.getElementsByTagName("trkseg");
    for (let s = 0; s < segments.length; s++) {
      const pts = [];
      const trkpts = segments[s].getElementsByTagName("trkpt");
      for (let i = 0; i < trkpts.length; i++) {
        const lat = Number(trkpts[i].getAttribute("lat"));
        const lon = Number(trkpts[i].getAttribute("lon"));
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
          continue;
        }
        const timeEl = trkpts[i].getElementsByTagName("time")[0];
        const when = timeEl ? new Date(timeEl.textContent) : new Date(NaN);
        pts.push({ point: [lat, lon], timestamp: when });
        if (!Number.isNaN(when.getTime())) {
          if (first === null || when < first) first = when;
          if (last === null || when > last) last = when;
        }
      }
      if (pts.length) {
        points += pts.length;
        paths.push({
          points: pts,
          color,
          name: segments.length > 1 ? "Segment " + (s + 1) : "Track",
          fullDatetime: true,
        });
      }
    }
    return { paths, points, first, last };
  }

  function fmtWhen(d) {
    return d ? d.toLocaleString() : "-";
  }

  class WicanGpxCard extends HTMLElement {
    static getStubConfig() {
      return { hours: DEFAULT_HOURS, title: "GPS track" };
    }

    setConfig(config) {
      if (!config || (!config.device_id && !config.url)) {
        throw new Error(TAG + ": set device_id (the WiCAN device) or url (a GPX file under /local/)");
      }
      const hours = Number(config.hours);
      this._config = {
        title: "title" in config ? config.title : "GPS track",
        height: Number(config.height) > 0 ? Number(config.height) : 320,
        color: config.color || "#1e88e5",
        hours: Number.isFinite(hours) && hours > 0 ? Math.min(hours, MAX_HOURS) : DEFAULT_HOURS,
        device_id: config.device_id || null,
        url: config.url || null,
      };
      this._loaded = false;
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      if (this._map) {
        this._map.hass = hass;
      }
      if (!this._loaded && !this._loading && this._config) {
        this._load();
      }
    }

    getCardSize() {
      return Math.ceil((this._config ? this._config.height : 320) / 50) + 1;
    }

    _render() {
      if (!this.shadowRoot) {
        this.attachShadow({ mode: "open" });
      }
      const root = this.shadowRoot;
      root.innerHTML = "";
      const style = document.createElement("style");
      style.textContent =
        ".map{display:block;width:100%;border-radius:0 0 var(--ha-card-border-radius,12px) var(--ha-card-border-radius,12px);overflow:hidden}" +
        ".line{padding:8px 16px 12px;font-size:13px;color:var(--secondary-text-color)}" +
        ".line.err{color:var(--error-color)}" +
        "h1{font-family:var(--ha-card-header-font-family,inherit);font-size:var(--ha-card-header-font-size,24px);font-weight:400;margin:0;padding:12px 16px 8px;color:var(--ha-card-header-color,var(--primary-text-color))}";
      const card = document.createElement("ha-card");
      if (this._config.title) {
        const h = document.createElement("h1");
        h.textContent = this._config.title;
        card.appendChild(h);
      }
      this._mapHost = document.createElement("div");
      this._mapHost.className = "map";
      this._mapHost.style.height = this._config.height + "px";
      this._line = document.createElement("div");
      this._line.className = "line";
      this._line.textContent = "Loading the track";
      card.appendChild(this._mapHost);
      card.appendChild(this._line);
      root.appendChild(style);
      root.appendChild(card);
      this._map = null;
    }

    _say(text, isError) {
      this._line.textContent = text;
      this._line.className = "line" + (isError ? " err" : "");
    }

    /* the frontend loads ha-map with its map card: mount one once */
    async _ensureMap() {
      if (!customElements.get("ha-map")) {
        if (typeof window.loadCardHelpers !== "function") {
          throw new Error("This Home Assistant has no card helpers to load the map with");
        }
        const helpers = await window.loadCardHelpers();
        const probe = helpers.createCardElement({ type: "map", entities: [] });
        probe.hass = this._hass;
        await customElements.whenDefined("ha-map");
      }
      if (!this._map) {
        this._map = document.createElement("ha-map");
        this._map.hass = this._hass;
        this._map.autoFit = true;
        this._map.renderPassive = false;
        this._map.interactiveZones = false;
        this._map.style.height = "100%";
        this._mapHost.appendChild(this._map);
      }
    }

    async _fitTo(paths) {
      const points = [];
      for (const path of paths) {
        for (const p of path.points) {
          points.push(p.point);
        }
      }
      if (!points.length) {
        return;
      }
      /* Leaflet arrives asynchronously inside the map element */
      for (let i = 0; i < 60; i++) {
        const L = this._map.Leaflet;
        const lm = this._map.leafletMap;
        if (L && lm) {
          lm.invalidateSize();
          lm.fitBounds(L.latLngBounds(points).pad(0.15), { maxZoom: 17 });
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    }

    async _trackUrl() {
      if (this._config.url) {
        return this._config.url;
      }
      const result = await this._hass.callService(
        "wican",
        "save_gpx",
        { device_id: this._config.device_id, hours: this._config.hours },
        undefined,
        false,
        true,
      );
      const response = result && result.response;
      if (!response || !response.url) {
        throw new Error("The device returned no track");
      }
      return response.url;
    }

    async _load() {
      this._loading = true;
      try {
        const url = await this._trackUrl();
        const reply = await fetch(url, { cache: "no-store" });
        if (!reply.ok) {
          throw new Error("The track file could not be read (HTTP " + reply.status + ")");
        }
        const track = parseGpx(await reply.text(), this._config.color);
        if (!track.points) {
          throw new Error("No position in the recorded log for this window");
        }
        await this._ensureMap();
        this._map.paths = track.paths;
        /* the map element fits itself to entities only (with none it
           centres on the home location): fit its Leaflet map to the
           track's own points once the map is up (seen on the bench) */
        this._map.autoFit = false;
        await this._fitTo(track.paths);
        this._say(
          track.points + " point" + (track.points === 1 ? "" : "s") +
          (track.paths.length > 1 ? " in " + track.paths.length + " segments" : "") +
          ", " + fmtWhen(track.first) + " to " + fmtWhen(track.last),
          false,
        );
      } catch (err) {
        this._say(err && err.message ? err.message : String(err), true);
      } finally {
        this._loading = false;
        this._loaded = true;
      }
    }
  }

  const register = () => {
    if (!customElements.get(TAG)) {
      customElements.define(TAG, WicanGpxCard);
    }
    window.customCards = window.customCards || [];
    if (!window.customCards.some((c) => c && c.type === TAG)) {
      window.customCards.push({
        type: TAG,
        name: "WiCAN GPS track",
        description: "The track a WiCAN recorded on its SD card, from the device's GPX export",
        preview: false,
        documentationURL: "https://github.com/meatpiHQ/ha-wican#the-gps-track-gpx",
      });
    }
  };

  /* The integration loads this file with the frontend's extra modules,
     ahead of the app bundle, and the bundle installs a custom-element
     registry of its own (scoped registries): an element defined before
     that is invisible to it and a card using it shows "Configuration
     error" (seen on the bench, 2026-11-06). Define once the app is there. */
  if (customElements.get("home-assistant")) {
    register();
  } else {
    customElements.whenDefined("home-assistant").then(register);
  }
})();
