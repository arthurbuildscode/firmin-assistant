// Prochain bus — serveur local (Node 20+, aucune dépendance).
// Lancer : node server.mjs   puis ouvrir http://localhost:8080
// En ligne, c'est api/index.mjs (Vercel) qui utilise la même logique (app.mjs).
//
// Firmin Assistant : seule différence avec le projet d'origine, l'adresse d'écoute est réglable
// par HOST (le service homeos-bus écoute sur 127.0.0.1:8081, jamais sur le réseau).

import http from "node:http";
import os from "node:os";
import { gerer } from "./app.mjs";

const PORT = Number(process.env.PORT || 8080);
const HOST = process.env.HOST || "0.0.0.0";

http.createServer(gerer).listen(PORT, HOST, () => {
  console.log(`Prochain bus : http://${HOST === "0.0.0.0" ? "localhost" : HOST}:${PORT}`);
  if (HOST !== "0.0.0.0") return;
  for (const ifs of Object.values(os.networkInterfaces()))
    for (const i of ifs) if (i.family === "IPv4" && !i.internal) console.log(`  depuis ton téléphone (même Wi-Fi) : http://${i.address}:${PORT}`);
});
