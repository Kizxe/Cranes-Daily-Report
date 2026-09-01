// Shared helpers for the Cranes Daily Report frontend.
const API = "/api";

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function today() {
  // Local date. toISOString() is UTC — in MYT (+08:00) it answered with yesterday's
  // date every morning until 8 AM, so the pickers opened on the wrong day.
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function qs(name) {
  return new URLSearchParams(location.search).get(name);
}

function pill(status) {
  const s = (status || "UNKNOWN").toUpperCase();
  // "NO DATA" would become two class names and lose its styling, so the class is
  // the status with every run of non-letters collapsed to a hyphen.
  return `<span class="pill ${s.replace(/[^A-Z]+/g, "-")}">${s}</span>`;
}

// Highlight the active nav link.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("header.top nav a").forEach((a) => {
    if (a.getAttribute("href") === location.pathname.split("/").pop()) a.classList.add("active");
  });
});
