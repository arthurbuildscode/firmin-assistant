// Coquille de l'appli : barre latérale (desktop), en-tête et onglets (mobile), menu de la personne, dock de saisie.
import { useState, type ReactNode } from "react";
import {
  BookOpen,
  Brain,
  CalendarDays,
  ChefHat,
  ChevronDown,
  Clock,
  Folder,
  History,
  House,
  LayoutGrid,
  LogOut,
  MessageCircle,
  MessageSquarePlus,
  RefreshCw,
  Settings,
  ShoppingCart,
  WifiOff,
} from "lucide-react";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { BandeauSecours } from "@/components/BandeauSecours";
import { messageErreur } from "@/lib/api";
import { heureDe, jourCourt } from "@/lib/dates";
import { naviguer, type Ecran } from "@/lib/routeur";
import { useAssistant, useSession } from "@/lib/session";
import { toast, useToasts } from "@/lib/toasts";
import { derniereCarte, magasin, useConversation } from "@/store/conversation";
import { Dock } from "./Dock";

export const ECRANS_INFO: Record<Ecran, { libelle: string; icone: typeof House }> = {
  conversation: { libelle: "Conversation", icone: MessageCircle },
  semaine: { libelle: "Semaine", icone: CalendarDays },
  courses: { libelle: "Courses", icone: ShoppingCart },
  repas: { libelle: "Repas", icone: ChefHat },
  echeances: { libelle: "Échéances", icone: Clock },
  journal: { libelle: "Journal", icone: BookOpen },
  fichiers: { libelle: "Fichiers", icone: Folder },
  memoire: { libelle: "Mémoire", icone: Brain },
  reglages: { libelle: "Réglages", icone: Settings },
  routines: { libelle: "Routines", icone: RefreshCw },
  plus: { libelle: "Plus", icone: LayoutGrid },
};

const GROUPES: { titre?: string; ecrans: Ecran[] }[] = [
  { titre: "Au quotidien", ecrans: ["conversation", "semaine", "courses", "repas", "echeances"] },
  { titre: "La famille", ecrans: ["journal", "fichiers", "memoire"] },
  { ecrans: ["reglages", "routines"] },
];
const ONGLETS: Ecran[] = ["conversation", "semaine", "courses", "repas", "plus"];

function useIndicateurs(): Partial<Record<Ecran, string>> {
  const messages = useConversation((e) => e.messages);
  const decisions = useConversation((e) => e.decisions);
  const courses = derniereCarte(messages, ["courses"]);
  const kd = derniereCarte(messages, ["kitchen_daily_proposition"]);
  const kdEnAttente = kd?.actions.some((a) => typeof a.confirmation === "number" && !decisions[a.confirmation]);
  const nonLus = useConversation((e) => e.nonLus);
  return {
    conversation: nonLus > 0 ? String(nonLus) : undefined,
    courses: courses && Number(courses.donnees.restants) > 0 ? String(courses.donnees.restants) : undefined,
    repas: kdEnAttente ? "À valider" : undefined,
  };
}

function lien(ecran: Ecran) {
  return (e: React.MouseEvent) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    naviguer(ecran);
  };
}

export const chemin = (ecran: Ecran) => (ecran === "conversation" ? "/" : `/${ecran}`);

function initiale(prenom?: string) {
  return (prenom || "?").trim().charAt(0).toUpperCase();
}

function MenuPersonne({ onDeconnexion, onConversations }: { onDeconnexion: () => void; onConversations: () => void }) {
  const session = useSession();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger className="inline-flex h-[30px] max-w-[150px] cursor-pointer items-center gap-1.5 rounded-full px-2.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground">
        <span className="ellip">{session.personne?.prenom}</span>
        <ChevronDown className="size-3.5 flex-none" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{session.personne?.role === "proprietaire" ? "Propriétaire du foyer" : "Membre du foyer"}</DropdownMenuLabel>
        <DropdownMenuItem onSelect={() => magasin.nouvelleConversation().then(() => naviguer("conversation")).catch((e) => toast(messageErreur(e)))}>
          <MessageSquarePlus />
          Nouvelle conversation
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onConversations}>
          <History />
          Conversations précédentes
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => naviguer("reglages")}>
          <Settings />
          Réglages
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={onDeconnexion}>
          <LogOut />
          Se déconnecter
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ListeConversations({ ouvert, setOuvert }: { ouvert: boolean; setOuvert: (o: boolean) => void }) {
  const conversations = useConversation((e) => e.conversations);
  const courante = useConversation((e) => e.conversationId);
  return (
    <Dialog open={ouvert} onOpenChange={setOuvert}>
      <DialogContent aria-describedby={undefined}>
        <DialogTitle>Conversations précédentes</DialogTitle>
        <div className="liste">
          {conversations.length === 0 && <p className="py-3 text-sm text-muted-foreground">Aucune autre conversation.</p>}
          {conversations.map((c) => {
            const d = new Date(c.maj);
            return (
              <button
                key={c.id}
                className="lien-plus"
                aria-current={c.id === courante}
                onClick={() => {
                  setOuvert(false);
                  naviguer("conversation");
                  magasin.ouvrir(c.id).catch((e) => toast(messageErreur(e)));
                }}
              >
                <span className="min-w-0 flex-1">
                  <span className="block ligne-t ellip">{c.titre || "Sans titre"}</span>
                  <span className="block ligne-s">
                    {Number.isNaN(d.getTime()) ? "" : `${jourCourt(d)} · ${heureDe(c.maj)}`}
                    {c.id === courante ? " · en cours" : ""}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function Coquille({ ecran, objet, children, onDeconnexion }: { ecran: Ecran; objet?: string; children: ReactNode; onDeconnexion: () => void }) {
  const assistant = useAssistant();
  const session = useSession();
  const indicateurs = useIndicateurs();
  const horsLigne = useConversation((e) => e.horsLigne);
  const toasts = useToasts();
  const [conversations, setConversations] = useState(false);
  const info = ECRANS_INFO[ecran];
  const Icone = ecran === "conversation" ? House : info.icone;
  const ouvrirConversations = () => {
    void magasin.listerConversations();
    setConversations(true);
  };
  return (
    <div>
      <aside className="barre-laterale" aria-label="Navigation principale">
        <div className="marque">
          <span className="logo">
            <House />
          </span>
          <b className="text-base font-semibold">{assistant}</b>
        </div>
        {GROUPES.map((g, i) => (
          <nav className="nav flex flex-col gap-0.5" key={i} aria-label={g.titre}>
            {g.titre ? <div className="nav-label">{g.titre}</div> : <div className="h-4" />}
            {g.ecrans.map((e) => {
              const I = ECRANS_INFO[e].icone;
              return (
                <a key={e} href={chemin(e)} onClick={lien(e)} aria-current={ecran === e ? "page" : undefined}>
                  <I className="size-[18px]" />
                  <span>{ECRANS_INFO[e].libelle}</span>
                  {indicateurs[e] && <span className="indic num">{indicateurs[e]}</span>}
                </a>
              );
            })}
          </nav>
        ))}
        <div className="pied-lateral">
          <div className="flex items-center gap-2.5 p-1">
            <span className="avatar">{initiale(session.personne?.prenom)}</span>
            <div className="min-w-0 flex-1">
              <div className="ellip text-sm font-semibold">{session.personne?.prenom}</div>
              <div className="text-xs text-muted-foreground">{session.personne?.role === "proprietaire" ? "Propriétaire" : "Membre"}</div>
            </div>
          </div>
        </div>
      </aside>

      <header className="entete">
        <span className="logo">
          <Icone />
        </span>
        <span className="ellip flex-1 text-base font-semibold tracking-tight">{ecran === "conversation" ? assistant : info.libelle}</span>
        <MenuPersonne onDeconnexion={onDeconnexion} onConversations={ouvrirConversations} />
      </header>

      <main className="principal">
        <div className="barre-haute">
          <MenuPersonne onDeconnexion={onDeconnexion} onConversations={ouvrirConversations} />
        </div>
        <div className="contenu" id="contenu" tabIndex={-1}>
          <BandeauSecours />
          {horsLigne && (
            <div className="mb-4 flex items-center gap-2 rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground" role="status">
              <WifiOff className="size-4" />
              Hors ligne : l’historique reste lisible, les messages partiront au retour du réseau.
            </div>
          )}
          {children}
        </div>
      </main>

      <nav className="onglets" aria-label="Navigation">
        {ONGLETS.map((e) => {
          const I = ECRANS_INFO[e].icone;
          const actif = ecran === e || (e === "plus" && !ONGLETS.includes(ecran));
          return (
            <a key={e} className="onglet" href={chemin(e)} onClick={lien(e)} aria-current={actif ? "page" : undefined}>
              <span className="pastille-o">
                <I className="size-[20px]" />
                {e === "conversation" && indicateurs.conversation && <span className="point-non-lu" aria-label={`${indicateurs.conversation} nouveau${Number(indicateurs.conversation) > 1 ? "x" : ""} message${Number(indicateurs.conversation) > 1 ? "s" : ""}`} />}
              </span>
              {ECRANS_INFO[e].libelle}
            </a>
          );
        })}
      </nav>

      <Dock ecran={ecran} objet={objet} />
      <ListeConversations ouvert={conversations} setOuvert={setConversations} />
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div className="toast" key={t.id}>
            {t.texte}
          </div>
        ))}
      </div>
    </div>
  );
}
