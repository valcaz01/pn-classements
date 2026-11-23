"""Lecture de présence par écho du WIRED du QG, pour le tableau des formations."""
import time

from habbo_http import ApiRequestError


CONTROLE = "/variables/global/controle_forma_qg"
PRESENCES = "/variables/user/presence_forma_qg/users"


class ErreurControle(ApiRequestError):
    def __init__(self, indication, chemin, status=None):
        super().__init__("response", status=status, endpoint=chemin)
        self.indication = indication


def valeur(objet, chemin):
    if objet is None:
        indication = "contrôle global introuvable" if chemin == CONTROLE else "variable de présence introuvable"
        raise ErreurControle(indication, chemin, 404)
    if isinstance(objet, dict):
        valeur_wired = objet.get("variable", objet)
        if isinstance(valeur_wired, dict):
            try:
                return int(str(valeur_wired.get("value")))
            except ValueError:
                pass
    indication = "valeur du contrôle illisible" if chemin == CONTROLE else "valeur de présence illisible"
    raise ErreurControle(indication, chemin)


def identite(fiche):
    return str(fiche.get("hid") or fiche["uid"])


class ControlePresenceQG:
    def __init__(self, api):
        self.api = api
        self.numero = None
        self.ids_wired = {}

    def page(self, page, taille):
        resultat = self.api(PRESENCES, {"order_by": "value", "order_dir": "desc", "page": page, "size": taille})
        if resultat is None:
            raise ErreurControle("variable de présence introuvable", PRESENCES, 404)
        if isinstance(resultat, list):
            return resultat
        if not isinstance(resultat, dict) or not isinstance(resultat.get("items"), list):
            raise ErreurControle("liste de présence illisible", PRESENCES)
        return resultat["items"]

    def lire_echos(self, numero):
        ids, publics = set(), set()
        page = 1
        while True:
            lignes = self.page(page, 100)
            for ligne in lignes:
                marque = valeur(ligne, PRESENCES)
                if marque < numero:
                    return ids, publics
                if marque > numero:
                    self.numero = None  # Une intervention a avancé le compteur entre écriture et lecture.
                    raise ErreurControle("contrôle modifié pendant la lecture", CONTROLE)
                joueur = ligne.get("user") or {}
                if not isinstance(joueur, dict):
                    raise ErreurControle("identifiant de présence illisible", PRESENCES)
                uid = str(joueur.get("id") or ligne.get("entity_id") or ligne.get("id") or "")
                if not uid.isdecimal() or uid in ids:
                    raise ErreurControle("identifiant de présence absent ou répété", PRESENCES)
                ids.add(uid)
                hid = joueur.get("unique_id") or joueur.get("uniqueId")
                if hid:
                    publics.add(hid)
            if len(lignes) < 100:
                return ids, publics
            page += 1

    def relever(self, fiches):
        if self.numero is None:
            actuel = valeur(self.api(CONTROLE), CONTROLE)
            derniers = self.page(1, 1)
            self.numero = max(actuel, valeur(derniers[0], PRESENCES) if derniers else 0)
        # Un numéro dont la réponse se perd ne doit jamais être réutilisé.
        self.numero += 1
        numero = self.numero
        reponse = self.api(CONTROLE, body={"value": str(numero)}, method="PATCH", write=True)
        if reponse is None:
            raise ErreurControle("écriture du contrôle introuvable", CONTROLE, 404)
        try:
            confirme = valeur(reponse, CONTROLE)
        except ErreurControle:
            # Un acquittement peut ne contenir aucune valeur. Vérifier sans rejouer l'écriture.
            reponse = self.api(CONTROLE)
            confirme = valeur(reponse, CONTROLE)
        if confirme != numero:
            self.numero = None
            raise ErreurControle("écriture du contrôle non confirmée", CONTROLE)
        time.sleep(3)  # Le PATCH confirme la globale avant la propagation aux avatars.
        ids, publics = self.lire_echos(numero)
        if not ids:
            time.sleep(4)
            ids, publics = self.lire_echos(numero)

        presents = set()
        suivis = {identite(f) for f in fiches}
        self.ids_wired = {k: v for k, v in self.ids_wired.items() if k in suivis}
        for fiche in fiches:
            hid, uid = identite(fiche), str(fiche["uid"])
            if hid in publics or uid in ids:
                presents.add(hid)
            elif ids and not uid.isdecimal() and len(publics) < len(ids):
                # Les qualifiantes sont indexées par identifiant public. Certains hôtels
                # ne renvoient que l'identifiant numérique dans la liste WIRED.
                dernier, numerique = self.ids_wired.get(hid, (float('-inf'), None))
                if not numerique and time.monotonic() - dernier >= 30:
                    profil = self.api("/variables_profile/user/users", {"unique_id": hid})
                    if profil is not None:
                        joueur = profil.get("user") if isinstance(profil, dict) else None
                        if (not isinstance(joueur, dict) or (joueur.get("unique_id") or joueur.get("uniqueId")) != hid
                                or not str(joueur.get("id")).isdecimal()):
                            raise ErreurControle("identifiant du candidat illisible", PRESENCES)
                        numerique = str(joueur["id"])
                    self.ids_wired[hid] = time.monotonic(), numerique
                if numerique in ids:
                    presents.add(hid)
        return numero, len(ids), presents
