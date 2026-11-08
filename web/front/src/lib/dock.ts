// Pré-remplir le champ de saisie depuis un écran (« Ajouter », « Note au journal »…), sans envoyer.
export function preremplirDock(texte: string): void {
  window.dispatchEvent(new CustomEvent("homeos:preremplir", { detail: texte }));
}
