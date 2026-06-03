import {
  PublicClientApplication,
  type AccountInfo,
  type Configuration,
} from "@azure/msal-browser";

import { config } from "../config";

const msalConfig: Configuration = {
  auth: {
    clientId: config.clientId,
    authority: `https://login.microsoftonline.com/${config.tenantId}`,
    redirectUri: window.location.origin,
  },
  cache: {
    cacheLocation: "localStorage",
  },
};

export const msalApp = new PublicClientApplication(msalConfig);

const tokenRequest = { scopes: [config.scope] };

let initialized = false;

/** Idempotently initialize MSAL and process any redirect response. Returns the
 *  signed-in account (if a redirect just completed or a session exists). */
export async function initAuth(): Promise<AccountInfo | null> {
  if (!initialized) {
    await msalApp.initialize();
    initialized = true;
  }
  const resp = await msalApp.handleRedirectPromise();
  if (resp?.account) {
    msalApp.setActiveAccount(resp.account);
    return resp.account;
  }
  const accounts = msalApp.getAllAccounts();
  if (accounts.length > 0) {
    const account = msalApp.getActiveAccount() ?? accounts[0];
    msalApp.setActiveAccount(account);
    return account;
  }
  return null;
}

export async function signIn(): Promise<void> {
  await msalApp.loginRedirect(tokenRequest);
}

export function signOut(account: AccountInfo): void {
  void msalApp.logoutRedirect({ account });
}

/** Acquire an access token silently, falling back to an interactive redirect
 *  when the session needs re-consent / re-auth (navigates away). */
export async function getToken(account: AccountInfo): Promise<string> {
  try {
    const res = await msalApp.acquireTokenSilent({ ...tokenRequest, account });
    return res.accessToken;
  } catch {
    await msalApp.acquireTokenRedirect(tokenRequest);
    throw new Error("redirecting for interactive auth");
  }
}
