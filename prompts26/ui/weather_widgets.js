// CREATE NEW FILE: public/ui/weather_widgets.js
// Weather widgets (dashboard-ready)
// Expects existing dashboard to call these and provide container elements.

export async function loadWeatherWidgets({ symbol = "SPY" } = {}) {
  const root = document.getElementById("weather-widgets");
  if (!root) return;

  root.innerHTML = `
    <div class="card">
      <div class="card-title">Weather Snapshot</div>
      <pre id="wx-snap" style="white-space:pre-wrap;margin:0;"></pre>
    </div>
    <div class="card">
      <div class="card-title">Active Weather Alerts</div>
      <pre id="wx-alerts" style="white-space:pre-wrap;margin:0;"></pre>
    </div>
    <div class="card">
      <div class="card-title">Weather Contribution (Base vs WX)</div>
      <pre id="wx-effect" style="white-space:pre-wrap;margin:0;"></pre>
    </div>
  `;

  async function jget(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return await r.json();
  }

  try {
    const snap = await jget(`/api/weather/snapshot?symbol=${encodeURIComponent(symbol)}`);
    document.getElementById("wx-snap").textContent = JSON.stringify(snap, null, 2);
  } catch (e) {
    document.getElementById("wx-snap").textContent = String(e);
  }

  try {
    const alerts = await jget(`/api/weather/alerts`);
    document.getElementById("wx-alerts").textContent = JSON.stringify(alerts, null, 2);
  } catch (e) {
    document.getElementById("wx-alerts").textContent = String(e);
  }

  try {
    const eff = await jget(`/api/weather/effect`);
    document.getElementById("wx-effect").textContent = JSON.stringify(eff, null, 2);
  } catch (e) {
    document.getElementById("wx-effect").textContent = String(e);
  }
}
