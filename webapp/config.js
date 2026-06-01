"use strict";

// Deployment configuration for the control panel.
// The backend is now wired in (no manual entry). Access is gated by Entra ID
// sign-in: only users in the tenant who are assigned to the app registration
// can obtain a token and reach the control API.
const CONFIG = {
  // Simulator container control API (external ingress).
  backendUrl: "https://ca-simulator.thankfulground-943b41a0.italynorth.azurecontainerapps.io",
  // Entra ID app registration (single tenant).
  tenantId: "39d764bc-ae80-46f9-b22c-6246cc5a20c2",
  clientId: "91351088-042c-4d80-a8dd-3983979d70b3",
  // Delegated scope exposed by the app registration.
  scope: "api://91351088-042c-4d80-a8dd-3983979d70b3/access_as_user",
};
