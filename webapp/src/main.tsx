import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { ThemeProvider } from "./theme/ThemeProvider";
import { ToastProvider } from "./components/Toasts";
import "./styles/index.css";

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("missing #root element");

createRoot(rootEl).render(
  <StrictMode>
    <ThemeProvider>
      <ToastProvider>
        <App />
      </ToastProvider>
    </ThemeProvider>
  </StrictMode>,
);
