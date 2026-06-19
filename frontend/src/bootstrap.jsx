function loadRuntimeConfig() {
  return new Promise((resolve) => {
    const script = document.createElement("script");
    script.src = "/config.js";
    script.async = false;
    script.onload = resolve;
    script.onerror = resolve;
    document.head.appendChild(script);
  });
}

loadRuntimeConfig().then(() => import("./main.jsx"));
