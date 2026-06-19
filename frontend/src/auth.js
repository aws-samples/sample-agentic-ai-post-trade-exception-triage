const config = typeof window !== "undefined" ? window.__TRIAGE_CONFIG || {} : {};
const auth = config.auth || {};

const TOKEN_KEY = "postTradeTriage.idToken";
const EXPIRES_AT_KEY = "postTradeTriage.idTokenExpiresAt";

function storage() {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function authUrl(path, params) {
  const search = new URLSearchParams(params);
  return `${auth.hostedUiDomain}${path}?${search.toString()}`;
}

function authRequestParams() {
  return {
    client_id: auth.userPoolClientId,
    response_type: "token",
    scope: "openid email profile",
    redirect_uri: auth.redirectUri || window.location.origin,
  };
}

function clearToken() {
  const store = storage();
  store?.removeItem(TOKEN_KEY);
  store?.removeItem(EXPIRES_AT_KEY);
}

export function isAuthEnabled() {
  return Boolean(auth.hostedUiDomain && auth.userPoolClientId);
}

export function completeHostedUiSignIn() {
  if (!isAuthEnabled() || typeof window === "undefined") return false;
  const hash = window.location.hash?.startsWith("#") ? window.location.hash.slice(1) : "";
  if (!hash) return false;
  const params = new URLSearchParams(hash);
  const token = params.get("id_token");
  if (!token) return false;
  const expiresIn = Number(params.get("expires_in") || "3600");
  const store = storage();
  store?.setItem(TOKEN_KEY, token);
  store?.setItem(EXPIRES_AT_KEY, String(Date.now() + Math.max(60, expiresIn - 60) * 1000));
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  return true;
}

export function getAuthToken() {
  if (!isAuthEnabled()) return "";
  const store = storage();
  const token = store?.getItem(TOKEN_KEY) || "";
  const expiresAt = Number(store?.getItem(EXPIRES_AT_KEY) || "0");
  if (!token || Date.now() >= expiresAt) {
    clearToken();
    return "";
  }
  return token;
}

export function isSignedIn() {
  return Boolean(getAuthToken());
}

export function signIn() {
  if (!isAuthEnabled() || typeof window === "undefined") return;
  window.location.assign(authUrl("/login", authRequestParams()));
}

export function signOut() {
  clearToken();
  if (!isAuthEnabled() || typeof window === "undefined") return;
  window.location.assign(
    authUrl("/logout", {
      client_id: auth.userPoolClientId,
      logout_uri: auth.logoutUri || window.location.origin,
    })
  );
}

export function authHeaders() {
  if (!isAuthEnabled()) {
    const err = new Error("Cognito Hosted UI is not configured for this protected demo.");
    err.status = 500;
    throw err;
  }
  const token = getAuthToken();
  if (!token) {
    const err = new Error("Sign in is required to use this protected demo.");
    err.status = 401;
    throw err;
  }
  return { authorization: token };
}
