import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";

// Point d'entrée : monte l'application React dans <div id="root">
createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>
);
