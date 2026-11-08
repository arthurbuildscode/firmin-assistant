// Point d'entrée de l'appli (lot 4b).
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { setNonce } from "get-nonce";
import App from "./App";
import { enregistrerServiceWorker } from "./lib/push";
import "./index.css";

// CSP stricte : les feuilles de style injectées par les composants (Radix) portent le nonce de la page (web/app.py).
const nonce = document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content;
if (nonce && !nonce.startsWith("__")) setNonce(nonce);

enregistrerServiceWorker();

createRoot(document.getElementById("racine")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
