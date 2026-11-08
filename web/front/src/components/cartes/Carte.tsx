// Aiguillage des cartes génératives (CONTRACT §14) : un composant par type ; un type inconnu n'est pas affiché
// (le cœur écarte déjà ce qu'il ne connaît pas).
import type { ComponentType } from "react";
import type { Carte as TCarte } from "@/lib/types";
import { ContexteCarteReact, type ContexteCarte } from "./actions";
import {
  CarteBus,
  CarteCapacite,
  CarteCapture,
  CarteComparatif,
  CarteDocumentSensible,
  CarteEnregistrement,
  CarteFichierClasse,
  CarteFichierRecu,
  CarteGenerique,
  CarteJournal,
  CarteReflexion,
  CarteSuggestions,
  CarteTexte,
} from "./divers";
import { CarteIdentifiants } from "./Identifiants";
import { CarteAgendaJour, CarteANoter, CarteBonjour, CarteBrief, CarteCreneaux, CarteEcheance, CarteEvenement, CarteMeteo } from "./jour";
import { CarteCourses, CarteKitchenDaily, CarteKitchenDailyExtras, CarteMenuInvites, CarteRepasSemaine } from "./maison";
import { CarteTransportsEtat, CarteTransportsProchains } from "./transports";

type Rendu = ComponentType<{ carte: TCarte }>;

export const RENDUS: Record<string, Rendu> = {
  bonjour: CarteBonjour,
  brief: CarteBrief,
  a_noter: CarteANoter,
  agenda_jour: CarteAgendaJour,
  evenement: CarteEvenement,
  courses: CarteCourses,
  repas_semaine: CarteRepasSemaine,
  kitchen_daily_proposition: CarteKitchenDaily,
  kitchen_daily_extras: CarteKitchenDailyExtras,
  echeance: CarteEcheance,
  comparatif: CarteComparatif,
  fichier_classe: CarteFichierClasse,
  document_sensible: CarteDocumentSensible,
  bus: CarteBus,
  meteo: CarteMeteo,
  transports_prochains: CarteTransportsProchains,
  transports_etat: CarteTransportsEtat,
  routine_resultat: ({ carte }) => <CarteTexte carte={carte} nom="routine_resultat" />,
  identifiants: CarteIdentifiants,
  reflexion: CarteReflexion,
  creneaux: CarteCreneaux,
  compte_rendu: ({ carte }) => <CarteTexte carte={carte} nom="compte_rendu" />,
  enregistrement: CarteEnregistrement,
  menu_invites: CarteMenuInvites,
  capacite_proposee: CarteCapacite,
  fichier_recu: CarteFichierRecu,
  journal: CarteJournal,
  suggestions: CarteSuggestions,
  capture: CarteCapture,
};

export function Carte({ carte, contexte }: { carte: TCarte; contexte?: ContexteCarte }) {
  if (!carte || typeof carte !== "object" || !carte.type) return null;
  const Rendu = RENDUS[carte.type] ?? CarteGenerique;
  const propre = { ...carte, actions: Array.isArray(carte.actions) ? carte.actions : [], donnees: carte.donnees || {} };
  return (
    <ContexteCarteReact.Provider value={contexte ?? {}}>
      <Rendu carte={propre} />
    </ContexteCarteReact.Provider>
  );
}
