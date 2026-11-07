/* Map for "Mjesta i profil". Click the map to set the place you are editing.
   Nothing here talks to a geocoder: the position comes from a click, a drag or the town list. */
(function () {
  "use strict";
  var el = document.getElementById("map");
  var dataEl = document.getElementById("map-data");
  if (!el || !dataEl || !window.L) { return; }

  var saved = JSON.parse(dataEl.textContent || "[]");
  var hint = document.getElementById("map-hint");
  var DEFAULT_HINT = hint ? hint.textContent : "";

  var map = L.map(el, { zoomControl: true }).setView([45.555, 18.6955], 11);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).addTo(map);

  function pin(cls) {
    return L.divIcon({ className: "", html: '<div class="pin ' + cls + '"></div>', iconSize: [22, 22], iconAnchor: [11, 11] });
  }

  // ---- saved places: a marker, and a rough circle (straight line) until a street map is downloaded.
  // With the street map the circle is replaced by the area that can really be reached (below).
  var streetsOn = el.getAttribute("data-streets") === "true";
  var accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#2257d6";
  var STREET_MODES = ["walk", "tram", "bike", "car"]; // the ways the street map can show
  var layers = {};
  var byId = {};
  var bounds = L.latLngBounds([]);
  saved.forEach(function (p) {
    byId[p.id] = p;
    var ll = [p.lat, p.lon];
    bounds.extend(ll);
    var marker = L.marker(ll, { icon: pin(p.active ? "" : "pin--off"), keyboard: false, title: p.label })
      .bindTooltip(p.label, { direction: "top", offset: [0, -10] }).addTo(map);
    var circle = p.radius_m > 0 ? L.circle(ll, {
      radius: p.radius_m, weight: 1.5, dashArray: streetsOn || !p.active ? "4 4" : null,
      color: p.active ? accent : "#888",
      fillOpacity: p.active ? 0.05 : 0.02, interactive: false
    }) : null;
    // a place used only by bus has no street area: it keeps its rough circle
    var onlyRough = !Object.keys(p.minutes || {}).some(function (m) { return STREET_MODES.indexOf(m) >= 0; });
    if (circle && (!streetsOn || onlyRough)) { circle.addTo(map); bounds.extend(circle.getBounds()); }
    layers[p.id] = { marker: marker, circle: circle };
  });
  if (bounds.isValid()) { map.fitBounds(bounds, { padding: [24, 24], maxZoom: 13 }); }

  // the circles Leaflet draws are decoration for the eye: screen readers get the note below the map
  var overlayPane = map.getPane("overlayPane");
  function hideDrawings() {
    overlayPane.querySelectorAll("svg, canvas").forEach(function (d) { d.setAttribute("aria-hidden", "true"); });
  }
  new MutationObserver(hideDrawings).observe(overlayPane, { childList: true, subtree: true });
  hideDrawings();

  // ---- the area that can really be reached, for the chosen way of travelling (street map)
  var areaLayer = L.layerGroup().addTo(map);
  var areaRenderer = L.canvas({ padding: 0.3 });
  var note = document.getElementById("map-area-note");
  var shown = 0; // answers can arrive out of order: only the newest one is drawn

  function onlyRoughPlace(id) {
    var p = byId[id];
    return !!p && !Object.keys(p.minutes || {}).some(function (m) { return STREET_MODES.indexOf(m) >= 0; });
  }

  function showArea(radio) {
    var mode = radio.value;
    var label = radio.parentNode.textContent.trim().toLowerCase();
    var ticket = ++shown;
    if (note) { note.textContent = "Učitavam područje…"; }
    fetch("/mjesta/podrucje?nacin=" + encodeURIComponent(mode), { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (ticket !== shown) { return; }
        areaLayer.clearLayers();
        var covered = {};
        var extent = L.latLngBounds([]);
        (data.places || []).forEach(function (p) {
          covered[p.id] = true;
          p.rects.forEach(function (r) {
            L.rectangle([[r[0], r[1]], [r[2], r[3]]], {
              renderer: areaRenderer, stroke: false, fillColor: accent, fillOpacity: 0.22, interactive: false
            }).addTo(areaLayer);
            extent.extend([r[0], r[1]]); extent.extend([r[2], r[3]]);
          });
        });
        // a place that uses this way but got no area (the map does not cover it) keeps its rough
        // circle; a place that does not use this way shows nothing for it
        Object.keys(layers).forEach(function (id) {
          var c = layers[id].circle;
          if (!c) { return; }
          var uses = byId[id] && byId[id].minutes && byId[id].minutes[mode];
          if (uses && !covered[id]) { if (!map.hasLayer(c)) { c.addTo(map); } }
          else if (!onlyRoughPlace(id)) { map.removeLayer(c); }
        });
        if (note) {
          var parts = (data.places || []).map(function (p) { return p.label + " (" + p.minutes + " min)"; });
          note.textContent = parts.length
            ? "Obojeno: dokle stižete " + label + " po ulicama: " + parts.join(", ") + "."
            : "Za ovaj način nema područja (nijedno mjesto ga ne koristi ili karta ne pokriva mjesto).";
        }
        if (extent.isValid() && bounds.isValid()) {
          map.fitBounds(extent.extend(bounds), { padding: [24, 24], maxZoom: 14, animate: false });
        }
      })
      .catch(function () {
        if (ticket !== shown) { return; }
        areaLayer.clearLayers();
        Object.keys(layers).forEach(function (id) {
          var p = byId[id];
          if (layers[id].circle && p && p.minutes && p.minutes[mode]) { layers[id].circle.addTo(map); }
        });
        if (note) { note.textContent = "Područje se nije moglo učitati; prikazan je grubi krug."; }
      });
  }

  if (streetsOn) {
    var radios = document.querySelectorAll('input[name="area-mode"]');
    radios.forEach(function (radio) {
      radio.addEventListener("change", function () { if (radio.checked) { showArea(radio); } });
    });
    var first = document.querySelector('input[name="area-mode"]:checked');
    if (first) { showArea(first); }
  }

  // ---- the place being edited
  var active = null; // { details, form, marker, id }

  function fields(form) {
    return {
      lat: form.querySelector("input[data-coord=lat]"),
      lon: form.querySelector("input[data-coord=lon]"),
      town: form.querySelector("[data-town-select]"),
      name: form.querySelector('input[name="label"]')
    };
  }

  function readPoint(form) {
    var f = fields(form);
    var lat = parseFloat((f.lat.value || "").replace(",", "."));
    var lon = parseFloat((f.lon.value || "").replace(",", "."));
    return (isFinite(lat) && isFinite(lon)) ? [lat, lon] : null;
  }

  function showPoint(ll, recentre) {
    if (!active) { return; }
    if (!ll) {
      if (active.marker) { map.removeLayer(active.marker); active.marker = null; }
      return;
    }
    if (!active.marker) {
      active.marker = L.marker(ll, { icon: pin("pin--edit"), draggable: true, keyboard: false }).addTo(map);
      active.marker.on("dragend", function () {
        var p = active.marker.getLatLng();
        writePoint(p.lat, p.lng, false);
      });
    } else {
      active.marker.setLatLng(ll);
    }
    if (recentre) { map.setView(ll, Math.max(map.getZoom(), 12)); }
  }

  function writePoint(lat, lon, recentre) {
    if (!active) { return; }
    var f = fields(active.form);
    f.lat.value = lat.toFixed(5);
    f.lon.value = lon.toFixed(5);
    f.town.value = ""; // a clicked point replaces the town choice
    showPoint([lat, lon], recentre);
  }

  function setActive(details) {
    clearActive();
    var form = details.querySelector("form[data-place-form]");
    if (!form) { return; }
    var id = details.getAttribute("data-place-id");
    active = { details: details, form: form, marker: null, id: id };
    if (layers[id]) { layers[id].marker.setOpacity(0); } // the edit marker replaces it
    showPoint(readPoint(form), true);
    var f = fields(form);
    var name = (f.name && f.name.value) || "mjesto";
    if (hint) { hint.textContent = "Kliknite na kartu da postavite „" + name + "“. Točku možete i povući."; }
    setTimeout(function () { map.invalidateSize(); }, 0);
  }

  function clearActive() {
    if (!active) { return; }
    if (active.marker) { map.removeLayer(active.marker); }
    if (layers[active.id]) { layers[active.id].marker.setOpacity(1); }
    active = null;
    if (hint) { hint.textContent = DEFAULT_HINT; }
  }

  document.querySelectorAll("details.place").forEach(function (d) {
    d.addEventListener("toggle", function () {
      if (d.open) {
        document.querySelectorAll("details.place[open]").forEach(function (other) { if (other !== d) { other.open = false; } });
        setActive(d);
      } else if (active && active.details === d) {
        clearActive();
      }
    });
  });

  map.on("click", function (e) {
    if (!active) {
      if (hint) { hint.textContent = "Najprije otvorite mjesto ili „Dodaj mjesto“, pa kliknite na kartu."; }
      return;
    }
    writePoint(e.latlng.lat, e.latlng.lng, false);
  });

  // ---- town list and typed coordinates move the marker too
  document.addEventListener("change", function (e) {
    if (!active || !active.form.contains(e.target)) { return; }
    var t = e.target;
    if (t.matches("[data-town-select]")) {
      var opt = t.selectedOptions[0];
      if (opt && opt.dataset.lat) {
        var f = fields(active.form);
        f.lat.value = parseFloat(opt.dataset.lat).toFixed(5);
        f.lon.value = parseFloat(opt.dataset.lon).toFixed(5);
        showPoint([parseFloat(opt.dataset.lat), parseFloat(opt.dataset.lon)], true);
      }
    } else if (t.matches("input[data-coord]")) {
      showPoint(readPoint(active.form), true);
    }
  });

  document.addEventListener("click", function (e) {
    var close = e.target.closest("[data-close-form]");
    if (close) {
      var d = close.closest("details.place");
      if (d) { d.open = false; }
    }
  });

  // a form that came back with errors is already open
  var open = document.querySelector("details.place[open]");
  if (open) { setActive(open); }
  window.addEventListener("resize", function () { map.invalidateSize(); });
})();
