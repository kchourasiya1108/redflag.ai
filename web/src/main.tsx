import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

try {
  const theme = localStorage.getItem("rf-theme");
  if (theme) document.documentElement.dataset.theme = theme;
} catch {
  /* storage blocked: follow the OS theme */
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
